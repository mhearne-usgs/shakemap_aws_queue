# stdlib imports
import logging
from datetime import datetime, timedelta

# third party imports
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import (Column, Integer, Float, String,
                        DateTime, ForeignKey, Boolean)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy_utils import database_exists, create_database

# We dynamically (not sure why?) create the base class for our objects
Base = declarative_base()

TIMEFMT = '%Y-%m-%dT%H:%M:%S'

MYSQL_TIMEOUT = 30

# association algorithm - any peak with:
# time > origin - TMIN and time < origin + TMAX
# AND
# distance < DISTANCE
TMIN = 60
TMAX = 180
DISTANCE = 500
P_TRAVEL_TIME = 4.2


class IncorrectDataTypesException(Exception):
    pass


class IncompleteConstructorException(Exception):
    pass


def get_session(url='sqlite:///:memory:', create_db=True):
    """Get a SQLAlchemy Session instance for input database URL.
    :param url:
      SQLAlchemy URL for database, described here:
        http://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls.
    :param create_db:
      Boolean indicating whether to create database from scratch.
    :returns:
      Sqlalchemy Session instance.
    """
    # Create a sqlite in-memory database engine
    if not database_exists(url):
        if create_db:
            create_database(url)
        else:
            msg = ('Database does not exist, will not create without '
                   'create_db turned on.')
            logging.error(msg)
            return None

    connect_args = {}
    if 'mysql' in url.lower():
        connect_args = {'connect_timeout': MYSQL_TIMEOUT}

    engine = create_engine(url, echo=False, connect_args=connect_args)
    Base.metadata.create_all(engine)

    # create a session object that we can use to insert and
    # extract information from the database
    Session = sessionmaker(bind=engine, autoflush=False)
    session = Session()

    return session


class Event(Base):
    """Class representing the "event" table in the database.

    """
    EVENT = {'eventid': String(64),
             'netid': String(32),
             'time': DateTime(),
             'lat': Float(),
             'lon': Float(),
             'depth': Float(),
             'magnitude': Float(),
             'locstring': String(1024),
             'lastrun': DateTime(),
             }
    __tablename__ = 'event'
    id = Column(Integer, primary_key=True)
    eventid = Column(EVENT['eventid'], index=True)
    netid = Column(EVENT['netid'])
    time = Column(EVENT['time'])
    lat = Column(EVENT['lat'])
    lon = Column(EVENT['lon'])
    depth = Column(EVENT['depth'])
    magnitude = Column(EVENT['magnitude'])
    locstring = Column(EVENT['locstring'])
    lastrun = Column(EVENT['lastrun'])

    queued_events = relationship("Queued", back_populates="event",
                                 cascade="all, delete, delete-orphan")

    @property
    def is_running(self):
        for queue in self.queued:
            if queue.is_running:
                return True
        return False

    @property
    def age_in_days(self):
        return (datetime.utcnow() - self.time) / timedelta(days=1)

    def __init__(self, **kwargs):
        """Instantiate an Event object from scratch (i.e., not from a query).

        Note: Although keyword arguments, all arguments below must be supplied.

        Args:
            eventid (str): Event ID of the form "us2020abcd".
            netid (str): The network code at the beginning of the eventid.
            time (datetime): Origin time, UTC.
            lat (float): Origin latitude.
            lon (float): Origin longitude.
            depth (float): Origin depth.
            magnitude (float): Origin magnitude.
            locstring (str): Description of earthquake location.
            lastrun (datetime): Set this to something like datetime(1900,1,1).

        Returns:
            Event: Instance of the Event object.
        """
        validate_inputs(self.EVENT, kwargs)

        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self):
        return (f'Event: {self.eventid}')


class Queued(Base):
    """Class representing the "queued" table in the database.

    """
    __tablename__ = 'queued'
    QUEUED = {'event_id': Integer(),
              'run_time': DateTime(),
              }
    id = Column(Integer, primary_key=True)
    event_id = Column(QUEUED['event_id'], ForeignKey('event.id'))
    run_time = Column(QUEUED['run_time'])

    event = relationship("Event", back_populates="queued_events")

    running_events = relationship("Running",
                                  back_populates="queued_event",
                                  cascade="all, delete, delete-orphan")

    def __init__(self, **kwargs):
        """Instantiate a Queued object from scratch (i.e., not from a query).

        Note: Although keyword arguments, all arguments below must be supplied.

        Args:
            event_id (int): ID of an existing (committed) Event object.
            run_time (datetime): Time (UTC) when event is scheduled to be run.

        Returns:
            Queued: Instance of the Queued object.
        """
        validate_inputs(self.QUEUED, kwargs)

        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def is_running(self):
        return len(self.running_events) > 0

    def __repr__(self):
        return (f'Queued: {self.event.eventid} {self.run_time}')


class Running(Base):
    """Class representing the "running" table in the database.

    """
    __tablename__ = 'running'
    RUNNING = {'queued_id': Integer(),
               'start_time': DateTime(),
               'success': Boolean(),
               }
    id = Column(Integer, primary_key=True)
    queued_id = Column(RUNNING['queued_id'], ForeignKey('queued.id'))
    start_time = Column(RUNNING['start_time'])
    success = Column(RUNNING['success'])

    queued_event = relationship("Queued", back_populates="running_events")

    def __init__(self, **kwargs):
        """Instantiate a Running object from scratch (i.e., not from a query).

        Note: Although keyword arguments, all arguments below must be supplied.

        Args:
            queued_id (int): ID of an existing (committed) Queued object.
            start_time (datetime): Time (UTC) when event began running.
            success (bool): Indicates whether the event has finished running successfully.

        Returns:
            Running: Instance of the Running object.
        """
        validate_inputs(self.RUNNING, kwargs)

        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def minutes_running(self):
        # return running time in minutes
        return (datetime.utcnow() - self.start_time) / timedelta(seconds=60)

    def __repr__(self):
        msg = (f'Running: {self.queued_event.event.eventid} '
               f'started at {self.start_time}')
        return (msg)


def validate_inputs(defdict, kwdict):
    """Validate all init() inputs against the python types of table columns.

    Args:
        defdict (dict): Dictionary containing the column
                        names/SQLAlchemy types.
        kwdict (dict): Dictionary containing the init() kwargs.

    Raises:
        IncompleteConstructorException: Not all kwargs are set.
        IncorrectDataTypesException: At least one of the kwargs is
                                     of the wrong type.
    """
    # first check that all required parameters are being set
    if not set(defdict.keys()) <= set(kwdict.keys()):
        msg = ('In Event constructor, all the following values must be set:'
               f'{str(list(defdict.keys()))}')
        raise IncompleteConstructorException(msg)

    errors = []
    for key, value in kwdict.items():
        ktype = defdict[key].python_type
        if not isinstance(value, ktype):
            errors.append(f'{key} must be of type {ktype}')
    if len(errors):
        msg = '\n'.join(errors)
        raise IncorrectDataTypesException(msg)
