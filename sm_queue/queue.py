# stdlib imports
import os.path
from datetime import datetime, timedelta
from urllib.request import urlopen
from urllib.parse import urlparse
from io import StringIO

# third party imports
import yaml
import boto3
from boto3.s3.transfer import TransferConfig

# local imports
from sm_queue.queue_db import Event, Queued, Running, get_session

MAX_SIZE = 4096
# Times can have either integer or floating point (preferred) seconds
TIMEFMT = '%Y-%m-%dT%H:%M:%S.%fZ'
ALT_TIMEFMT = '%Y-%m-%dT%H:%M:%SZ'

QUEUE_URL = 'QUEUE_URL'
DB_URL = 'DB_URL'
S3_BUCKET_URL = 'S3_BUCKET_URL'

MB = 1048576
PAYLOAD_LIMIT = 5242880

NETWORKS = {'us': 'National Earthquake Information Center',
            'ci': ('Southern California Seismic Network '
                   '(Caltech/USGS Pasadena and Partners) '
                   'and Southern California'),
            'nc': ('Northern California Seismic System '
                   '(UC Berkeley, USGS Menlo Park, and Partners)'),
            'nn': 'Nevada Seismological Laboratory',
            'uu': 'University of Utah',
            'uw': 'Pacific Northwest Seismic Network'
            }


def point_inside_polygon(x, y, poly):
    # determine if a point is inside a given polygon or not
    # Polygon is a list of (x,y) pairs.
    # http://www.ariel.com.au/a/python-point-int-poly.html
    n = len(poly)
    inside = False

    p1x, p1y = poly[0]
    for i in range(n + 1):
        p2x, p2y = poly[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y

    return inside


def str_to_seconds(tstring):
    """ Convert time strings to seconds. Strings can be of the
    form:
        <int>   (ninutes)
        <int>m  (minutes)
        <int>h  (hours)
        <int>d  (days)
        <int>y  (years)
    Args:
        tstring (str): An integer followed by an (optional)
                       'm', 'h', 'd', 'y'.
    Returns
        int: The number of seconds represented by the input string. If
        the string is unadorned and represents a negative number, -1
        is returned.
    Raises:
        ValueError: If the value cannot be converted to seconds.
    """
    if tstring.endswith('m'):
        secs = 60 * int(tstring.replace('m', ''))
    elif tstring.endswith('h'):
        secs = 60 * 60 * int(tstring.replace('h', ''))
    elif tstring.endswith('d'):
        secs = 24 * 60 * 60 * int(tstring.replace('d', ''))
    elif tstring.endswith('y'):
        secs = 365 * 24 * 60 * 60 * int(tstring.replace('y', ''))
    else:
        secs = 60 * int(tstring)
        if secs < 0:
            secs = -1

    return secs


def get_polygon(x, y, config):
    # return a polygon dictionary or empty dictionary
    for polygon in config['queue']['polygons']:
        if point_inside_polygon(x, y, polygon['polygon']):
            return polygon
    return {}


def get_config():
    # get the environment variable telling us where our queue config is
    if QUEUE_URL not in os.environ:
        msg = (f"Could not find queue config url "
               f"environment variable {QUEUE_URL}")
        raise NameError(msg)
    queue_url = os.environ[QUEUE_URL]
    try:
        with urlopen(queue_url) as fh:
            data = fh.read().decode('utf8')

        io_obj = StringIO(data)
        config = yaml.safe_load(io_obj)

        # turn the polygon strings into lists of x,y tuples
        for polygon in config['queue']['polygons']:
            coord_strings = polygon['polygon'].split(',')
            coords = [float(c) for c in coord_strings]
            polytuples = list(zip(coords[1:-1:2], coords[0:-2:2]))
            polygon['polygon'] = polytuples

        # time strings
        for key in ['old_event_age', 'future_event_age', 'max_trigger_wait']:
            tstr = config['queue'][key]
            config['queue'][key] = str_to_seconds(tstr)

        # fix other time strings
        for repeat in config['queue']['repeats']:
            tstrings = repeat['times'].split(',')
            tsecs = [str_to_seconds(t) for t in tstrings]
            repeat['times'] = tsecs

        if 'network_delays' in config['queue']:
            for network in config['queue']['network_delays']:
                network['delay'] = str_to_seconds(network['delay'])
        else:
            config['queue']['network_delays'] = []

        return config
    except Exception as e:
        raise e


def insert_event(eventdict):
    """Attempt to insert an event into the ShakeMap Queue database.

    Args:
        eventdict: Event dictionary, with the following keys:
                   - eventid ('us2020abcd')
                   - netid ('us')
                   - ids (alternate id list)
                   - time (datetime in UTC)
                   - latitude
                   - longitude
                   - depth
                   - magnitude
                   - locstring
    """
    if DB_URL not in os.environ:
        raise KeyError(f"Database URL {DB_URL} not in environment.")
    db_url = os.environ[DB_URL]
    config = get_config()
    session = get_session(db_url)

    # First ask if the event is already in the queue
    allids = [eventdict['eventid']] + eventdict['ids']
    for eid in allids:
        eventobj = session.query(Event).filter(Event.eventid == eid).first()
        if eventobj is None:
            continue
        break

    # if we found it, does it have a queue?
    if eventobj is not None and len(eventobj.queued_events):
        # get the last run time in the queue
        rtimes = sorted([e.run_time for e in eventobj.queued_events])
        last_time = rtimes[-1]
        dt = timedelta(seconds=config['queue']['max_trigger_wait'])
        new_time = last_time + dt
        queue = Queued(event_id=eventobj.id, run_time=new_time)
        session.add(queue)
        new_event = {Event.time: eventdict['time'],
                     Event.lat: eventdict['latitude'],
                     Event.lon: eventdict['longitude'],
                     Event.depth: eventdict['depth'],
                     Event.magnitude: eventdict['magnitude'],
                     }
        session.query(Event).filter(Event.eventid == eid).update(new_event)
        session.commit()
        session.close()
        return True

    # no queued events, what is the age of this event?
    dt_old = timedelta(seconds=config['queue']['old_event_age'])
    too_old = eventdict['time'] < (datetime.utcnow() - dt_old)
    dt_future = timedelta(seconds=config['queue']['future_event_age'])
    too_future = eventdict['time'] > (datetime.utcnow() + dt_future)
    if too_old or too_future:
        session.close()
        return False

    # does this event pass the minimum magnitude check?
    minmag = config['queue']['minmag']
    polygon = get_polygon(eventdict['longitude'],
                          eventdict['latitude'], config)
    if len(polygon):
        minmag = polygon['magnitude']
    if eventdict['magnitude'] < minmag:
        session.close()
        return False

    # it does, let's insert the event, and set up some runtimes
    event = Event(eventid=eventdict['eventid'],
                  netid=eventdict['netid'],
                  time=eventdict['time'],
                  lat=eventdict['latitude'],
                  lon=eventdict['longitude'],
                  depth=eventdict['depth'],
                  magnitude=eventdict['magnitude'],
                  locstring=eventdict['locstring'],
                  lastrun=datetime(1900, 1, 1)
                  )
    session.add(event)
    session.commit()

    # find what the repeat times are for this magnitude
    times = []
    for repeat in config['queue']['repeats']:
        if event.magnitude > repeat['mag']:
            times = repeat['times']
    # add a runtime for right now, and then however many are
    # configured.
    session.add(Queued(event_id=event.id, run_time=datetime.utcnow()))
    for rtime in times:
        run_time = datetime.utcnow() + timedelta(seconds=rtime)
        session.add(Queued(event_id=event.id, run_time=run_time))
    session.commit()
    session.close()


def clean_running():
    if DB_URL not in os.environ:
        raise KeyError(f"Database URL {DB_URL} not in environment.")
    db_url = os.environ[DB_URL]
    config = get_config()
    session = get_session(db_url)
    running_events = session.query(Running).filter().all()
    for running in running_events:
        threshold = config['queue']['max_process_time'] / 60
        if running.success:
            session.query(Running).\
                filter(Running.id == running.id).delete()
            session.query(Queued).\
                filter(Queued.id == running.queued_id).delete()

        else:
            if running.minutes_running < threshold:
                continue
            # now we have a process that's running long and hasn't returned
            # a success condition.
            # cancel the running entry
            session.query(Running).\
                filter(Running.id == running.id).delete()
            # keep the queued entry so it will run again (?)
            # notify the developers about the problem
            notify_developers()
    session.commit()


def notify_developers():
    return True


def run_events():
    if DB_URL not in os.environ:
        raise KeyError(f"Database URL {DB_URL} not in environment.")
    db_url = os.environ[DB_URL]
    config = get_config()
    session = get_session(db_url)

    # first make sure we're not over our max running limit
    nrunning = session.query(Running).count()
    if nrunning >= config['queue']['max_running']:
        session.close()
        return False

    # get all events that are due to run
    condition = Queued.run_time < datetime.utcnow()
    queued = session.query(Queued).filter(condition).all()
    for queue in queued:
        # write event.xml to S3 bucket
        write_event_to_s3(queue.event)
        # clear out all runtimes earlier than the current run time
        # for this event
        session.query(Queued).\
            filter(Queued.event_id == queue.event_id).\
            filter(Queued.run_time < queue.run_time).delete()
        qid = queue.id
        stime = datetime.utcnow()
        running = Running(queued_id=qid, start_time=stime, success=False)
        session.add(running)
        session.commit()
        start_shakemap(queue.event.eventid)
    session.close()


def write_event_to_s3(event):
    eventxml = get_event_xml(event)
    transfer_config = TransferConfig(multipart_threshold=PAYLOAD_LIMIT,
                                     max_concurrency=10,
                                     multipart_chunksize=PAYLOAD_LIMIT,
                                     use_threads=True)
    s3_client = boto3.client('s3')
    bucket = get_bucket()
    key = '/'.join(['events', event.eventid, 'input', 'event.xml'])
    bucket = get_bucket()
    extra = {'ACL': 'public-read',
             'ContentType': 'text/json'}
    s3_client.upload_fileobj(eventxml, bucket, key,
                             ExtraArgs=extra,
                             Config=transfer_config)
    return True


def get_event_xml(event):
    network = ''
    if event.netid in NETWORKS:
        network = NETWORKS[event.netid]
    xmlstr = (f'<earthquake id="{event.eventid}" netid="{event.netid}" '
              f'lat="{event.lat}" lon="{event.lon}" '
              f'depth="{event.depth}" mag="{event.magnitude}" '
              f'locstring="event.locstring" '
              f'network="{network}" '
              f'time="{event.time.strftime(TIMEFMT)}" / >')
    return xmlstr.encode('utf8')


def get_bucket():
    if S3_BUCKET_URL not in os.environ:
        raise KeyError(f"S3 Bucket URL {S3_BUCKET_URL} not in environment.")
    bucket_url = os.getenv('S3_BUCKET_URL')  # this will need to be set
    parts = urlparse(bucket_url)
    locparts = parts.netloc.split('.')
    bucket_id = locparts[0]
    return bucket_id


def start_shakemap(eventid):
    return True
