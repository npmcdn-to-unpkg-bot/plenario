from hashlib import md5
from itertools import groupby
from operator import itemgetter
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from plenario import db

import json
import traceback
from collections import namedtuple
from datetime import datetime
from hashlib import md5
from itertools import groupby
from operator import itemgetter
from uuid import uuid4

import sqlalchemy as sa
from flask_bcrypt import Bcrypt
from geoalchemy2 import Geometry
from sqlalchemy import Column, String, Boolean, Date, DateTime, Text, func
from sqlalchemy import Table, select, Integer
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import synonym
from sqlalchemy.types import NullType

from plenario.utils.helpers import get_size_in_degrees, slugify

bcrypt = Bcrypt()

class MetaTable(db.Model):
    __tablename__ = 'meta_master'
    # limited to 50 chars elsewhere
    dataset_name = db.Column(db.String(100), nullable=False)
    human_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    source_url = db.Column(db.String(255))
    source_url_hash = db.Column(db.String(32), primary_key=True)
    view_url = db.Column(db.String(255))
    attribution = db.Column(db.String(255))
    # Spatial and temporal boundaries of observations in this dataset
    obs_from = db.Column(db.Date)
    obs_to = db.Column(db.Date)
    bbox = db.Column(Geometry('POLYGON', srid=4326))
    # TODO: Add restriction list ['daily' etc.]
    update_freq = db.Column(db.String(100), nullable=False)
    last_update = db.Column(db.DateTime)
    date_added = db.Column(db.DateTime)
    # The names of our "special" fields
    observed_date = db.Column(db.String, nullable=False)
    latitude = db.Column(db.String)
    longitude = db.Column(db.String)
    location = db.Column(db.String)
    # if False, then do not display without first getting administrator approval
    approved_status = db.Column(db.Boolean)
    contributor_name = db.Column(db.String)
    contributor_organization = db.Column(db.String)
    contributor_email = db.Column(db.String)
    result_ids = db.Column(ARRAY(db.String))
    column_names = db.Column(JSONB)  # storage format {'<COLUMN_NAME>': '<COLUMN_TYPE>'}

    def __init__(self, url, human_name, observed_date,
                 approved_status=False, update_freq='yearly',
                 latitude=None, longitude=None, location=None,
                 attribution=None, description=None,
                 column_names=None,
                 contributor_name=None, contributor_email=None,
                 contributor_organization=None, **kwargs):
        """
        :param url: url where CSV or Socrata dataset with this dataset resides
        :param human_name: Nicely formatted name to display to people
        :param business_key: Name of column with the dataset's unique ID
        :param observed_date: Name of column with the datset's timestamp
        :param approved_status: Has an admin signed off on this dataset?
        :param update_freq: one of ['daily', 'weekly', 'monthly', 'yearly']
        :param latitude: Name of col with latitude
        :param longitude: Name of col with longitude
        :param location: Name of col with location formatted as (lat, lon)
        :param attribution: Text describing who maintains the dataset
        :param description: Text describing the dataset.
        """
        def curried_slug(name):
            if name is None:
                return None
            else:
                return slugify(unicode(name), delim=u'_')

        # Some combination of columns from which we can derive a point in space.
        assert(location or (latitude and longitude))
        # Frontend validation should have slugified column names already,
        # but enforcing it here is nice for testing.
        self.latitude = curried_slug(latitude)
        self.longitude = curried_slug(longitude)
        self.location = curried_slug(location)

        assert human_name
        self.human_name = human_name
        # Known issue: slugify fails hard on Non-ASCII
        self.dataset_name = kwargs.get('dataset_name',
                                       curried_slug(human_name)[:50])

        assert observed_date
        self.observed_date = curried_slug(observed_date)

        assert url
        self.source_url, self.source_url_hash = url, md5(url).hexdigest()
        self.view_url = self._get_view_url_val(url)

        assert update_freq
        self.update_freq = update_freq

        # Can be None. In practice,
        # frontend validation makes sure these are always passed along.
        self.description, self.attribution = description, attribution

        # Expect a list of strings
        self.column_names = column_names

        # Boolean
        self.approved_status = approved_status

        self.contributor_name = contributor_name
        self.contributor_organization = contributor_organization
        self.contributor_email = contributor_email

    @staticmethod
    def _get_view_url_val(url):
        trunc_index = url.find('.csv?accessType=DOWNLOAD')
        if trunc_index == -1:
            return None
        else:
            return url[:trunc_index]

    def __repr__(self):
        return '<MetaTable %r (%r)>' % (self.human_name, self.dataset_name)

    def meta_tuple(self):
        PointDataset = namedtuple('PointDataset', 'name date lat lon loc')

        basic_info = PointDataset(name=self.dataset_name,
                                  date=self.observed_date,
                                  lat=self.latitude,
                                  lon=self.longitude,
                                  loc=self.location)
        return basic_info

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def column_info(self):
        return self.point_table.c

    @property
    def point_table(self):
        try:
            return self._point_table
        except AttributeError:
            self._point_table = Table(self.dataset_name, Base.metadata,
                                      autoload=True, extend_existing=True)
            return self._point_table

    @classmethod
    def attach_metadata(cls, rows):
        """
        Given a list of dicts that include a dataset_name,
        add metadata about the datasets to each dict.
        :param rows: List of dict-likes with a dataset_name attribute
        """
        dataset_names = [row['dataset_name'] for row in rows]

        # All the metadata attributes that we can pull in unaltered
        as_is_attr_names = ['dataset_name', 'human_name', 'date_added',
                            'obs_from', 'obs_to', 'last_update',
                            'attribution', 'description', 'update_freq',
                            'view_url', 'source_url',
                            'contributor_name', 'contributor_email',
                            'contributor_organization']
        as_is_attrs = [getattr(cls, name) for name in as_is_attr_names]

        # Bounding box is the exception. We need to process it a bit.
        bbox = func.ST_AsGeoJSON(cls.bbox)

        # Put our as-is and processed attributes together
        attr_names = as_is_attr_names + ['bbox']
        attrs = as_is_attrs + [bbox]

        # Make the DB call
        result = db.session.query(*attrs). \
            filter(cls.dataset_name.in_(dataset_names))
        meta_list = [dict(zip(attr_names, row)) for row in result]

        # We need to coerce datetimes to strings
        date_attrs = ['date_added', 'obs_from', 'obs_to']
        for row in meta_list:
            row['bbox'] = json.loads(row['bbox'])
            for attr in date_attrs:
                row[attr] = str(row[attr])

        # Align the original list and metadata list...
        meta_list = sorted(meta_list, key=itemgetter('dataset_name'))
        to_coalesce = sorted(rows, key=itemgetter('dataset_name'))

        # and coalesce them.
        for original, meta in zip(to_coalesce, meta_list):
            original.update(meta)

        return to_coalesce

    # Return a list of [
    # {'dataset_name': 'Foo',
    # 'items': [{'datetime': dt, 'count': int}, ...] } ]
    @classmethod
    def timeseries_all(cls, table_names, agg_unit, start, end, geom=None, ctrees=None):
        # For each table in table_names, generate a query to be unioned
        selects = []
        for name in table_names:

            # If we have condition trees specified, apply them.
            # .get will return None for those datasets who don't have filters
            ctree = ctrees.get(name) if ctrees else None
            table = cls.get_by_dataset_name(name)
            ts_select = table.timeseries(agg_unit, start, end, geom, ctree)
            selects.append(ts_select)

        # Union the time series selects to get a panel
        panel_query = sa.union(*selects) \
            .order_by('dataset_name') \
            .order_by('time_bucket')
        panel_vals = db.session.execute(panel_query)

        panel = []
        for dataset_name, ts in groupby(panel_vals, lambda row: row.dataset_name):

            # ts gets closed after it's been iterated over once,
            # so we need to store the rows somewhere to iterate over them twice.
            rows = [row for row in ts]
            # If no records were found, don't include this dataset
            if all([row.count == 0 for row in rows]):
                continue

            ts_dict = {'dataset_name': dataset_name,
                       'items': []}

            for row in rows:
                ts_dict['items'].append({
                    'datetime': row.time_bucket.date(),  # UTC time
                    'count': row.count
                })
                # Aggregate top-level count across all time slices.
                ts_dict['count'] = sum([i['count'] for i in ts_dict['items']])
            panel.append(ts_dict)

        return panel

    # Information about all point datasets
    @classmethod
    def index(cls):
        try:
            q = session.query(cls.dataset_name)
            q = q.filter(cls.approved_status == True)
            names = [result.dataset_name for result in q.all()]
        except ProgrammingError:
            # Handles a case that causes init_db to crash.
            # Validator calls index when initializing, prevents this call
            # from raising an error when the database is empty.
            names = []
        return names

    @classmethod
    def narrow_candidates(cls, dataset_names, start, end, geom=None):
        """
        :param dataset_names: Names of point datasets to be considered
        :return names: Names of point datasets whose bounding box and date range
                       interesects with the given bounds.
        """
        # Filter out datsets that don't intersect the time boundary
        q = db.session.query(cls.dataset_name) \
            .filter(cls.dataset_name.in_(dataset_names), cls.date_added != None,
                    cls.obs_from < end,
                    cls.obs_to > start)

        # or the geometry boundary
        if geom:
            intersecting = cls.bbox.ST_Intersects(func.ST_GeomFromGeoJSON(geom))
            q = q.filter(intersecting)

        return [row.dataset_name for row in q.all()]

    @classmethod
    def get_by_dataset_name(cls, name):
        foo = db.session.query(cls).filter(cls.dataset_name == name).first()
        return foo

    def get_bbox_center(self):
        sel = select([func.ST_AsGeoJSON(func.ST_centroid(self.bbox))])
        result = db.session.execute(sel)
        # returns [lon, lat]
        return json.loads(result.first()[0])['coordinates']

    def update_date_added(self):
        now = datetime.now()
        if self.date_added is None:
            self.date_added = now
        self.last_update = now

    def make_grid(self, resolution, geom=None, conditions=None, obs_dates={}):
        """
        :param resolution: length of side of grid square in meters
        :type resolution: int
        :param geom: string representation of geojson fragment
        :type geom: str
        :param conditions: conditions on columns to filter on
        :type conditions: list of SQLAlchemy binary operations
                          (e.g. col > value)
        :return: grid: result proxy with all result rows
                 size_x, size_y: the horizontal and vertical size
                                    of the grid squares in degrees
        """
        if conditions is None:
            conditions = []

        # We need to convert resolution (given in meters) to degrees
        # - which is the unit of measure for EPSG 4326 -
        # - in order to generate our grid.
        center = self.get_bbox_center()
        # center[1] is longitude
        size_x, size_y = get_size_in_degrees(resolution, center[1])

        # Generate a count for each resolution by resolution square
        t = self.point_table
        q = db.session.query(func.count(t.c.hash),
                          func.ST_SnapToGrid(t.c.geom, size_x, size_y)
                          .label('squares')) \
            .filter(*conditions) \
            .group_by('squares')

        if obs_dates:
            q = q.filter(t.c.point_date >= obs_dates['lower'])
            q = q.filter(t.c.point_date <= obs_dates['upper'])

        if geom:
            q = q.filter(t.c.geom.ST_Within(func.ST_GeomFromGeoJSON(geom)))

        return db.session.execute(q), size_x, size_y

    # Return select statement to execute or union
    def timeseries(self, agg_unit, start, end, geom=None, column_filters=None):
        # Reading this blog post
        # http://no0p.github.io/postgresql/2014/05/08/timeseries-tips-pg.html
        # inspired this implementation.
        t = self.point_table

        # Special case for the 'quarter' unit of aggregation.
        step = '3 months' if agg_unit == 'quarter' else '1 ' + agg_unit

        # Create a CTE to represent every time bucket in the timeseries
        # with a default count of 0
        day_generator = func.generate_series(func.date_trunc(agg_unit, start),
                                             func.date_trunc(agg_unit, end),
                                             step)
        defaults = select([sa.literal_column("0").label('count'),
                           day_generator.label('time_bucket')]) \
            .alias('defaults')

        where_filters = [t.c.point_date >= start, t.c.point_date <= end]
        if column_filters is not None:
            # Column filters has to be iterable here, because the '+' operator
            # behaves differently for SQLAlchemy conditions. Instead of
            # combining the conditions together, it would try to build
            # something like :param1 + <column_filters> as a new condition.
            where_filters += [column_filters]

        # Create a CTE that grabs the number of records contained in each time
        # bucket. Will only have rows for buckets with records.
        actuals = select([func.count(t.c.hash).label('count'),
                          func.date_trunc(agg_unit, t.c.point_date).
                         label('time_bucket')]) \
            .where(sa.and_(*where_filters)) \
            .group_by('time_bucket')

        # Also filter by geometry if requested
        if geom:
            contains = func.ST_Within(t.c.geom, func.ST_GeomFromGeoJSON(geom))
            actuals = actuals.where(contains)

        # Need to alias to make it usable in a subexpression
        actuals = actuals.alias('actuals')

        # Outer join the default and observed values
        # to create the timeseries select statement.
        # If no observed value in a bucket, use the default.
        name = sa.literal_column("'{}'".format(self.dataset_name)) \
            .label('dataset_name')
        bucket = defaults.c.time_bucket.label('time_bucket')
        count = func.coalesce(actuals.c.count, defaults.c.count).label('count')
        ts = select([name, bucket, count]). \
            select_from(defaults.outerjoin(actuals, actuals.c.time_bucket == defaults.c.time_bucket))

        return ts

    def timeseries_one(self, agg_unit, start, end, geom=None, column_filters=None):
        ts_select = self.timeseries(agg_unit, start, end, geom, column_filters)
        rows = db.session.execute(ts_select.order_by('time_bucket'))

        header = [['count', 'datetime']]
        # Discard the name attribute.
        rows = [[count, time_bucket.date()] for _, time_bucket, count in rows]
        return header + rows


class DataDump(db.Model):
    __tablename__ = "plenario_datadump"
    id = Column(String(32), primary_key=True)
    request = Column(String(32), nullable=False)
    part = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)
    data = Column(Text)

    def __init__(self, id, request, part, total, data):
        self.id = id
        self.request = request
        self.part = part
        self.total = total
        self.data = data

    def get_data(self):
        return self.data

    def get_id(self):
        return self.id


class Workers(db.Model):
    __tablename__ = "plenario_workers"
    name = Column(String(32), primary_key=True)
    timestamp = Column(String(32), nullable=False)
    uptime = Column(Integer, nullable=False)
    job = Column(String(32), nullable=True)
    jobcounter = Column(Integer, nullable=False)

    def __init__(self, name, uptime):
        self.name = name
        self.timestamp = str(datetime.now())
        self.uptime = uptime
        self.jobcounter = 0

    def check_in(self):
        self.timestamp = str(datetime.now())

    def register_job(self, job):
        self.job = job

    def deregister_job(self):
        if self.job:
            self.jobcounter += 1
        self.job = None

    @classmethod
    def purge(cls):
        try:
            session.query(cls).delete()
            session.commit()
        except Exception as e:
            session.rollback()
            traceback.print_exc()
            print("Problem purging plenario_workers: {}".format(e))