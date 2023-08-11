# coding: utf-8
"""
This module contains tasks that can be scheduled by huey and/or run as flask cli commands.
(The cli commands could eventually be moved to another module).
"""

import csv
import datetime
import json
from functools import wraps

import click
import flask
import sqlalchemy as sa
import sqlalchemy.dialects.sqlite as sqlite
from flask import current_app as app
from huey import crontab
from huey.contrib.mini import MiniHuey

import feedi.models as models
import feedi.sources as sources
from feedi.app import create_huey_app
from feedi.models import db

feed_cli = flask.cli.AppGroup('feed')

huey = MiniHuey()


def huey_task(*huey_args):
    "Wraps a function to make a it a MiniHuey task that is run inside a flask app context."

    huey_decorator = huey.task(*huey_args)

    def with_context(f):

        @wraps(f)
        def decorator(*args, **kwargs):
            # run the task inside an app context and log start and finish
            app = create_huey_app()
            with app.app_context():
                fargs = ' '.join(args)
                fkwargs = ' '.join([f'{k}={v}' for (k, v) in kwargs.items()])
                app.logger.info("STARTING %s %s %s", f.__name__, fargs, fkwargs)

                f(*args, **kwargs)

                app.logger.info("FINISHED %s %s %s", f.__name__, fargs, fkwargs)

        return decorator

    def composed_decorator(f):
        return huey_decorator(with_context(f))

    return composed_decorator


@feed_cli.command('sync')
@huey_task(crontab(minute=app.config['SYNC_FEEDS_CRON_MINUTES']))
def sync_all_feeds():
    feeds = db.session.execute(db.select(models.Feed.name, models.Feed.type)).all()

    tasks = []
    for feed in feeds:
        if feed.type == models.Feed.TYPE_RSS:
            task = sync_rss_feed(feed.name)
        elif feed.type == models.Feed.TYPE_MASTODON_ACCOUNT:
            task = sync_mastodon_feed(feed.name)
        else:
            app.logger.error("unknown feed type %s", feed.type)
            continue

        tasks.append(task)

    # wait for concurrent tasks to finish before returning
    for task in tasks:
        try:
            task.get()
        except:
            app.logger.exception("failure during async task %s", task)
            continue


@huey_task()
def sync_mastodon_feed(feed_name):
    db_feed = db.session.scalar(db.select(models.Feed).filter_by(name=feed_name))
    latest_entry = db_feed.entries.order_by(models.Entry.remote_updated.desc()).first()
    args = {}
    if latest_entry:
        # there's some entry on db, this is not the first time we're syncing
        # get all toots since the last seen one
        args['newer_than'] = latest_entry.remote_id
    else:
        # if there isn't any entry yet, get the "first page" of toots from the timeline
        args['limit'] = app.config['MASTODON_FETCH_LIMIT']

    app.logger.debug("Fetching toots %s", args)
    toots = sources.mastodon.fetch_toots(server_url=db_feed.server_url,
                                         access_token=db_feed.access_token,
                                         **args)
    upsert_entries(db_feed.id, toots)


@huey_task()
def sync_rss_feed(feed_name):
    db_feed = db.session.scalar(db.select(models.Feed).filter_by(name=feed_name))
    utcnow = datetime.datetime.utcnow()

    if db_feed.last_fetch and utcnow - db_feed.last_fetch < datetime.timedelta(minutes=app.config['RSS_SKIP_RECENTLY_UPDATED_MINUTES']):
        app.logger.info('skipping recently synced feed %s', db_feed.name)
        return

    app.logger.debug('fetching rss %s %s', db_feed.name, db_feed.url)
    entries, feed_data, etag, modified, = sources.rss.fetch(db_feed.url,
                                                            db_feed.last_fetch,
                                                            app.config['RSS_SKIP_OLDER_THAN_DAYS'],
                                                            etag=db_feed.etag, modified=db_feed.modified_header)

    db_feed.last_fetch = utcnow
    db_feed.etag = etag
    db_feed.modified_header = modified
    db_feed.raw_data = json.dumps(feed_data)
    db.session.merge(db_feed)
    db.session.commit()

    upsert_entries(db_feed.id, entries)


# NOTE most of this code should probably live in a db related module eg models
def upsert_entries(feed_id, entries_values):
    """
    Insert or update the given feed's entries based on the values in the list.
    An insert and commit is emitted for each entry.
    """
    utcnow = datetime.datetime.utcnow()
    for values in entries_values:
        # upsert to handle already seen entries.
        # updated time set explicitly as defaults are not honored in manual on_conflict_do_update
        values['updated'] = utcnow
        values['feed_id'] = feed_id
        db.session.execute(
            sqlite.insert(models.Entry).
            values(**values).
            on_conflict_do_update(("feed_id", "remote_id"), set_=values)
        )

        # inline commit to avoid sqlite locking when fetching parallel feeds
        db.session.commit()


@huey_task(crontab(minute="0", hour='*/2'))
def set_frequency_ranks():
    """
    Classify each feed into "buckets" according to how frequently its entries are published.
    This is used to feature more prominently the ones that are less frequent (i.e. preferring occasional
    blog posts over spammy news articles or social media updates).
    """

    two_weeks_ago = datetime.datetime.now() - datetime.timedelta(days=14)
    q = db.select(models.Feed, sa.func.count(models.Entry.id))\
        .join(models.Entry)\
        .filter(models.Entry.remote_updated >= two_weeks_ago)\
        .group_by(models.Feed)\
        .order_by(sa.func.count(models.Entry.id))

    # this is admittedly a dumb/arbitrary way of setting scores
    # but I'd rather see how it works for a while than spending time in figuring
    # a fancy weighting algorithm
    for feed, count in db.session.execute(q).all():
        if count <= 2:
            # weekly or less
            rank = 1
        elif count < 5:
            # couple of times a week
            rank = 2
        elif count < 15:
            # up to once a day
            rank = 3
        elif count < 45:
            # up to 3 times a day
            rank = 3
        elif count < 300:
            #~ 20 a day
            rank = 4
        else:
            # more
            rank = 5
        feed.frequency_rank = rank

    db.session.commit()


@feed_cli.command('debug')
@click.argument('url')
def debug_feed(url):
    sources.rss.pretty_print(url)


@feed_cli.command('load')
@click.argument("file")
def create_test_feeds(file):
    "Load feeds from a local csv file."

    with open(file) as csv_file:
        for attrs in csv.reader(csv_file):
            feed_type = attrs[0]
            feed_name = attrs[1]
            query = db.select(models.Feed).where(models.Feed.name == feed_name)
            db_feed = db.session.execute(query).first()
            if db_feed:
                app.logger.info('skipping already existent %s', feed_name)
                continue

            if feed_type == models.Feed.TYPE_RSS:
                url = attrs[2]
                db_feed = models.RssFeed(name=feed_name,
                                         url=url,
                                         icon_url=sources.rss.detect_feed_icon(url))

            elif feed_type == models.Feed.TYPE_MASTODON_ACCOUNT:
                server_url = attrs[2]
                access_token = attrs[3]

                db_feed = models.MastodonAccount(name=feed_name,
                                                 server_url=server_url,
                                                 access_token=access_token,
                                                 icon_url=sources.mastodon.fetch_avatar(server_url, access_token))

            else:
                app.logger.error("unknown feed type %s", attrs[0])
                continue

            db.session.add(db_feed)
            app.logger.info('added %s', db_feed)

    db.session.commit()


app.cli.add_command(feed_cli)
