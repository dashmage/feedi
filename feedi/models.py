import datetime
import math

import sqlalchemy as sa
from flask_sqlalchemy import SQLAlchemy

# TODO consider adding explicit support for url columns

db = SQLAlchemy()


def init_db(app):
    db.init_app(app)

    @sa.event.listens_for(db.engine, 'connect')
    def on_connect(dbapi_connection, _connection_record):
        # registers a custom function that can be used during queries
        # in this case to sort the feed based on the post frequency of the sources
        dbapi_connection.create_function('freq_bucket', 1, Feed.freq_bucket)

    db.create_all()


class Feed(db.Model):
    """
    TODO
    """
    __tablename__ = 'feeds'

    TYPE_RSS = 'rss'
    TYPE_MASTODON_ACCOUNT = 'mastodon'

    id = sa.Column(sa.Integer, primary_key=True)
    type = sa.Column(sa.String, nullable=False)

    name = sa.Column(sa.String, unique=True, index=True)
    icon_url = sa.Column(sa.String)

    created = sa.Column(sa.TIMESTAMP, nullable=False, default=datetime.datetime.utcnow)
    updated = sa.Column(sa.TIMESTAMP, nullable=False,
                        default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    entries = sa.orm.relationship("Entry", back_populates="feed",
                                  cascade="all, delete-orphan", lazy='dynamic')
    raw_data = sa.Column(sa.String, doc="The original feed data received from the feed, as JSON")
    folder = sa.Column(sa.String, index=True)
    views = sa.Column(sa.Integer, default=0, nullable=False,
                      doc="counts how many times articles of this feed have been read. ")

    __mapper_args__ = {'polymorphic_on': type,
                       'polymorphic_identity': 'feed'}

    def __repr__(self):
        return f'<Feed {self.name}>'

    @staticmethod
    def freq_bucket(count):
        """
        To be used as a DB function, this returns a "rank" of the feed based on how
        many posts we've seen (assuming the count is for the last 2 weeks).
        This rank classifies the feeds so the least frequent posters are displayed more
        prominently.
        """
        # this is pretty hacky but it's low effort and servers for experimentation
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
        else:
            # more
            rank = 5
        return rank


class RssFeed(Feed):
    url = sa.Column(sa.String)
    last_fetch = sa.Column(sa.TIMESTAMP)
    etag = sa.Column(
        sa.String, doc="Etag received on last parsed rss, to prevent re-fetching if it hasn't changed.")
    modified_header = sa.Column(
        sa.String, doc="Last-modified received on last parsed rss, to prevent re-fetching if it hasn't changed.")

    __mapper_args__ = {'polymorphic_identity': 'rss'}


class MastodonAccount(Feed):
    # TODO this could be a fk to a separate table with client/secret
    # to share the feedi app across accounts of that same server
    server_url = sa.Column(sa.String)
    access_token = sa.Column(sa.String)

    __mapper_args__ = {'polymorphic_identity': 'mastodon'}


class Entry(db.Model):
    """
    TODO
    """
    __tablename__ = 'entries'

    id = sa.Column(sa.Integer, primary_key=True)

    feed_id = sa.orm.mapped_column(sa.ForeignKey("feeds.id"))
    feed = sa.orm.relationship("Feed", back_populates="entries")
    remote_id = sa.Column(sa.String, nullable=False,
                          doc="The identifier of this entry in its source feed.")

    title = sa.Column(sa.String, nullable=False)
    username = sa.Column(sa.String, index=True)
    user_url = sa.Column(sa.String, doc="The url of the user that authored the entry.")
    avatar_url = sa.Column(
        sa.String, doc="The url of the avatar image to be displayed for the entry.")

    body = sa.Column(
        sa.String, doc="The content to be displayed in the feed preview. HTML is supported. For article entries, it would be an excerpt of the full article content.")
    entry_url = sa.Column(
        sa.String, doc="The URL of this entry in the source. For link aggregators this would be the comments page.")
    content_url = sa.Column(
        sa.String, doc="The URL where the full content can be fetched or read. For link aggregators this would be the article redirect url.")
    media_url = sa.Column(sa.String, doc="URL of a media attachement or preview.")

    created = sa.Column(sa.TIMESTAMP, nullable=False, default=datetime.datetime.utcnow)
    updated = sa.Column(sa.TIMESTAMP, nullable=False,
                        default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    remote_created = sa.Column(sa.TIMESTAMP, nullable=False)
    remote_updated = sa.Column(sa.TIMESTAMP, nullable=False)

    deleted = sa.Column(sa.TIMESTAMP, index=True)
    favorited = sa.Column(sa.TIMESTAMP, index=True)
    pinned = sa.Column(sa.TIMESTAMP, index=True)

    raw_data = sa.Column(sa.String, doc="The original entry data received from the feed, as JSON")

    # mastodon specific
    reblogged_by = sa.Column(sa.String)

    __table_args__ = (sa.UniqueConstraint("feed_id", "remote_id"),
                      sa.Index("entry_updated_ts", remote_updated.desc()))

    def __repr__(self):
        return f'<Entry {self.feed_id}/{self.remote_id}>'

    @classmethod
    def _filtered_query(cls, deleted=None, favorited=None,
                        feed_name=None, username=None, folder=None):
        """
        Return a base Entry query applying any combination of filters.
        """

        query = db.select(cls)

        if deleted:
            query = query.filter(cls.deleted.is_not(None))
        else:
            query = query.filter(cls.deleted.is_(None))

        if favorited:
            query = query.filter(cls.favorited.is_not(None))

        if feed_name:
            query = query.filter(cls.feed.has(name=feed_name))

        if folder:
            query = query.filter(cls.feed.has(folder=folder))

        if username:
            query = query.filter(cls.username == username)

        return query

    @classmethod
    def select_pinned(cls, **kwargs):
        "Return the full list of pinned entries considering the optional filters."
        query = cls._filtered_query(**kwargs)\
                   .filter(cls.pinned.is_not(None))\
                   .order_by(cls.pinned.desc())

        return db.session.scalars(query).all()

    @classmethod
    def select_page_chronologically(cls, limit, older_than, **filters):
        """
        Return up to `limit` entries in reverse chronological order, considering the given
        `filters`.
        """
        query = cls._filtered_query(**filters)

        if older_than:
            # FIXME move float conversion outside
            query = query.filter(cls.remote_updated < older_than)

        query = query.order_by(cls.remote_updated.desc()).limit(limit)
        return db.session.scalars(query).all()

    @classmethod
    def select_page_by_frequency(cls, limit, start_at, page, **filters):
        """
        Order entries by least frequent feeds first then reverse-chronologically for entries in the same
        frequency rank. The results are also put in 48 hours 'buckets' so we only highlight articles
        during the first couple of days after their publication. (so as to not have fixed stuff in the
        top of the timeline for too long).
        """
        query = cls._filtered_query(**filters)

        # count the amount of entries per feed seen in the last two weeks and map the count to frequency "buckets"
        # (see the models.Feed.freq_bucket function) to be used in the order by clause of the next query
        two_weeks_ago = datetime.datetime.now() - datetime.timedelta(days=14)
        subquery = db.select(Feed.id, sa.func.freq_bucket(sa.func.count(cls.id)).label('rank'))\
                     .join(cls)\
                     .filter(cls.remote_updated >= two_weeks_ago)\
                     .group_by(Feed)\
                     .subquery()

        # by ordering by a "bucket" of "is it older than 48hs?" we effectively get all entries in the last 2 days first,
        # without having to filter out the rest --i.e. without truncating the feed
        last_48_hours = start_at - datetime.timedelta(hours=48)
        query = query.join(Feed)\
                     .join(subquery, subquery.c.id == Feed.id)\
                     .order_by(
                         (start_at > cls.remote_updated) & (
                             cls.remote_updated < last_48_hours),
                         subquery.c.rank,
                         cls.remote_updated.desc()).limit(limit)

        return db.paginate(query, page=page)