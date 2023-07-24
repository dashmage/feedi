# coding: utf-8

import csv
import datetime
import time

import favicon
import feedparser
import requests
import sqlalchemy.dialects.sqlite as sqlite
from bs4 import BeautifulSoup

import feedi.models as models
from feedi.database import db

# TODO parametrize in command or app config
UPDATE_AFTER_MINUTES = 5


class BaseParser:
    """
    TODO
    """

    FIELDS = ['title', 'title_url', 'avatar_url', 'username', 'body',
              'media_url', 'remote_id', 'remote_created', 'remote_updated']

    @staticmethod
    def is_compatible(_feed_url, _feed_data):
        """
        Returns whether this class knows how to parse entries from the given feed.
        The base parser should reasonably work with any rss feed.
        """
        # subclasses need to override this. This base class can be used directly without testing for compatibility
        raise NotImplementedError

    # TODO review if this has a reasonable purpose vs just passing everything on the parse fun
    def __init__(self, feed, db_feed, logger):
        self.feed = feed
        self.db_feed = db_feed
        self.logger = logger

    def parse(self, entry):
        """
        TODO
        """
        if 'link' not in entry or 'summary' not in entry:
            raise ValueError(f"entry seems malformed {entry}")

        result = {}
        for field in self.FIELDS:
            method = 'parse_' + field
            result[field] = getattr(self, method)(entry)
        return result

    def parse_title(self, entry):
        return entry['title']

    def parse_title_url(self, entry):
        return entry['link']

    def parse_username(self, entry):
        return entry.get('author')

    def parse_avatar_url(self, entry):
        url = entry.get('source', {}).get('icon')
        if url and requests.head(url).ok:
            self.logger.debug('found entry-level avatar %s', url)
            return url

    def parse_body(self, entry):
        soup = BeautifulSoup(entry['summary'], 'lxml')

        # remove images in case there are any inside a paragraph
        for tag in soup('img'):
            tag.decompose()

        # take the first couple of paragraphs that have text
        result = ''
        if soup.p:
            result += str(soup.p.extract())
        if soup.p:
            result += '\n' + str(soup.p.extract())
        return result

    def parse_media_url(self, entry):
        # first try to get it in standard feed fields
        if 'media_thumbnail' in entry:
            return entry['media_thumbnail'][0]['url']

        if 'media_content' in entry and entry['media_content'][0].get('type') == 'image':
            return entry['media_content'][0]['url']

        # else try to extract it from the summary html
        soup = BeautifulSoup(entry['summary'], 'lxml')
        if soup.img:
            return soup.img['src']

        self.logger.debug('didnt found media in feed, trying with meta origin meta tags %s', entry['link'])
        soup = BeautifulSoup(requests.get(entry['link']).content, 'lxml')
        meta_tag = soup.find("meta", property="og:image", content=True) or soup.find("meta", property="twitter:image", content=True)
        if meta_tag:
            return meta_tag['content']

    def parse_remote_id(self, entry):
        return entry['id']

    def parse_remote_created(self, entry):
        return to_datetime(entry['published_parsed'])

    def parse_remote_updated(self, entry):
        return to_datetime(entry['updated_parsed'])


class LinkAggregatorParser(BaseParser):
    """
    TODO
    """
    @staticmethod
    def is_compatible(_feed_url, feed_data):
        # TODO test this with lemmy as well
        KNOWN_AGGREGATORS = ['lobste.rs', 'reddit.com', 'news.ycombinator.com']
        return any([domain in feed_data['feed']['link'] for domain in KNOWN_AGGREGATORS])


class MastodonUserParser(BaseParser):
    """
    TODO
    """
    @staticmethod
    def is_compatible(_feed_url, feed_data):
        return 'mastodon' in feed_data['feed'].get('generator', '').lower()

    def parse_title(self, _entry):
        return self.feed['feed']['title']


class GithubFeedParser(BaseParser):
    """
    TODO
    """
    @staticmethod
    def is_compatible(feed_url, _feed_data):
        return 'github.com' in feed_url and 'private.atom' in feed_url

    def parse_body(self, _entry):
        return None

    def parse_avatar_url(self, entry):
        return entry['media_thumbnail'][0]['url']

    def parse_media_url(self, _entry):
        return None


class GoodreadsFeedParser(BaseParser):
    """
    TODO
    """
    @staticmethod
    def is_compatible(feed_url, _feed_data):
        return 'goodreads.com' in feed_url and '/home/index_rss' in feed_url

    def parse_body(self, _entry):
        return None

    def parse_media_url(self, _entry):
        return None


def sync_all_feeds(app):
    db_feeds = db.session.execute(db.select(models.Feed)).all()
    for (db_feed,) in db_feeds:
        sync_feed(app, db_feed)

    db.session.commit()


def sync_feed(app, db_feed):
    if db_feed.last_fetch and datetime.datetime.utcnow() - db_feed.last_fetch < datetime.timedelta(minutes=UPDATE_AFTER_MINUTES):
        app.logger.info('skipping up to date feed %s', db_feed.name)
        return

    app.logger.info('fetching %s', db_feed.name)
    db_feed.last_fetch = datetime.datetime.utcnow()
    feed = feedparser.parse(db_feed.url)

    if 'updated_parsed' in feed and db_feed.last_fetch and datetime.datetime.utcnow() - to_datetime(feed['updated_parsed']) < datetime.timedelta(minutes=UPDATE_AFTER_MINUTES):
        app.logger.info('skipping up to date feed %s', db_feed.name)
        return

    parser_cls = BaseParser
    # FIXME this is hacky, we aren't enforcing an order which may be necessary
    for cls in BaseParser.__subclasses__():
        if cls.is_compatible(db_feed.url, feed):
            parser_cls = cls
            break
    parser = parser_cls(feed, db_feed, app.logger)

    app.logger.info('parsing %s with %s', db_feed.name, parser_cls)
    for entry in feed['entries']:
        try:
            values = parser.parse(entry)
        except Exception as e:
            app.logger.exception("parsing raised error: %s", e)
            continue

        # upsert to handle already seen entries.
        # updated time set explicitly as defaults are not honored in manual on_conflict_do_update
        values['updated'] = db_feed.last_fetch
        values['feed_id'] = db_feed.id
        db.session.execute(
            sqlite.insert(models.Entry).
            values(**values).
            on_conflict_do_update(("feed_id", "remote_id"), set_=values)
        )


def to_datetime(struct_time):
    return datetime.datetime.fromtimestamp(time.mktime(struct_time))


def detect_feed_icon(app, feed, url):
    icon_url = feed['feed'].get('icon', feed['feed'].get('webfeeds_icon'))
    if icon_url and requests.head(icon_url).ok:
        app.logger.debug("using feed icon: %s", icon_url)
    else:
        favicons = favicon.get(feed['feed'].get('link', url))
        # if multiple formats, assume the .ico is the canonical one if present
        favicons = [f for f in favicons if f.format == 'ico'] or favicons
        icon_url = favicons[0].url
        app.logger.debug('using favicon %s', icon_url)

    return icon_url


def debug_feed(url):
    feed = feedparser.parse(url)
    import pprint
    pp = pprint.PrettyPrinter(depth=10)
    pp.pprint(feed)


def create_test_feeds(app):
    with open('feeds.csv') as csv_file:
        for feed_name, url in csv.reader(csv_file):
            query = db.select(models.Feed).where(models.Feed.name == feed_name)
            db_feed = db.session.execute(query).first()
            if db_feed:
                app.logger.info('skipping already existent %s', feed_name)
                continue

            feed = feedparser.parse(url)
            db_feed = models.Feed(name=feed_name, url=url, icon_url=detect_feed_icon(app, feed, url))
            db.session.add(db_feed)
            app.logger.info('added %s', db_feed)

    db.session.commit()