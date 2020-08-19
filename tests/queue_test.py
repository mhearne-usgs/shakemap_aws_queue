#!/usr/bin/env python

import pathlib
import os.path
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest import mock
import time

from sm_queue.queue_db import (get_session, Event, Queued, Running)
from sm_queue.queue import (get_config, get_polygon,
                            insert_event, run_events,
                            clean_running)


def test_config():
    queue_file = pathlib.Path(__file__).parent / 'data' / 'queue.yaml'
    queue_url = queue_file.resolve().as_uri()
    os.environ['QUEUE_URL'] = queue_url
    config = get_config()
    nzy, nzx = -43.967215, 170.4192763
    polygon = get_polygon(nzx, nzy, config)
    assert polygon['name'] == 'nz'
    assert polygon['magnitude'] == 4.89

    # Antarctic - should not be in any polygon
    anty, antx = -74.158104, -94.3164677
    polygon = get_polygon(antx, anty, config)
    assert not len(polygon)


def test_insert():
    queue_file = pathlib.Path(__file__).parent / 'data' / 'queue.yaml'
    queue_url = queue_file.resolve().as_uri()
    os.environ['QUEUE_URL'] = queue_url
    eventdict = {'eventid': 'us2020abcd',
                 'netid': 'us',
                 'ids': ['ci12345678'],
                 'time': datetime.utcnow(),
                 'latitude': 34.123,
                 'longitude': -118.123,
                 'depth': 30.1,
                 'magnitude': 7.8,
                 'locstring': 'Somewhere in California'}

    try:
        tdir = tempfile.mkdtemp()
        dbfile = pathlib.Path(tdir) / 'test.db'
        # dbfile.touch()
        dburl = dbfile.as_uri().replace('file:', 'sqlite:/')
        os.environ['DB_URL'] = dburl
        insert_event(eventdict)
        session = get_session(dburl)
        event = session.query(Event).first()
        assert len(event.queued_events) == 3
        session.close()
        # make another version of this event with a different ID
        eventdict2 = {'eventid': 'ci12345678',  # same event, different ID
                      'netid': 'us',
                      'ids': ['us2020abcd'],
                      'time': datetime.utcnow(),
                      'latitude': 34.456,  # changed the latitude a little
                      'longitude': -118.123,
                      'depth': 30.1,
                      'magnitude': 7.8,
                      'locstring': 'Somewhere in California'}
        insert_event(eventdict2)
        session = get_session(dburl)
        event2 = session.query(Event).first()
        assert len(event2.queued_events) == 4
        assert event2.lat == eventdict2['latitude']

        # make a really old event
        eventdict3 = {'eventid': 'nc12345678',  # same event, different ID
                      'netid': 'us',
                      'ids': [],
                      'time': datetime(2010, 1, 1),
                      'latitude': 34.456,  # changed the latitude a little
                      'longitude': -118.123,
                      'depth': 30.1,
                      'magnitude': 7.8,
                      'locstring': 'Somewhere in California'}
        result = insert_event(eventdict3)
        assert not result

        # make an event far in the future
        eventdict4 = {'eventid': 'nc12345678',  # same event, different ID
                      'netid': 'us',
                      'ids': [],
                      'time': datetime.utcnow() + timedelta(days=1000),
                      'latitude': 34.456,  # changed the latitude a little
                      'longitude': -118.123,
                      'depth': 30.1,
                      'magnitude': 7.8,
                      'locstring': 'Somewhere in California'}
        result = insert_event(eventdict4)
        assert not result

        # insert a really small event
        eventdict4 = {'eventid': 'nc12345678',  # same event, different ID
                      'netid': 'us',
                      'ids': [],
                      'time': datetime.utcnow(),
                      'latitude': 34.456,  # changed the latitude a little
                      'longitude': -118.123,
                      'depth': 30.1,
                      'magnitude': 1.1,
                      'locstring': 'Somewhere in California'}
        result = insert_event(eventdict4)
        assert not result
    except Exception as e:
        msg = f'Something unexpected happened! {str(e)}'
        raise AssertionError(msg)
    finally:
        shutil.rmtree(tdir)


class MockS3Client(object):
    """Mock boto3 S3 client"""

    def upload_fileobj(self, eventxml, bucket, key,
                       ExtraArgs=None,
                       Config=None):
        return None


class MockEmailClient(object):
    "Mock boto3 ses client"

    def send_email(self,
                   Destination=None,
                   Message=None,
                   ReplyToAddresses=None,
                   ReturnPath=None,
                   ReturnPathArn=None,
                   Source=None,
                   SourceArn=None,
                   ):
        return {'MessageId': 'EXAMPLE78603177f-7a5433e7-8edb-42ae-af10-f0181f34d6ee-000000',
                'ResponseMetadata': 'metadata'}


def test_run_events():
    queue_file = pathlib.Path(__file__).parent / 'data' / 'queue.yaml'
    queue_url = queue_file.resolve().as_uri()
    os.environ['QUEUE_URL'] = queue_url
    os.environ['S3_BUCKET_URL'] = 'foo'
    eventdict = {'eventid': 'us2020abcd',
                 'netid': 'us',
                 'ids': ['ci12345678'],
                 'time': datetime.utcnow(),
                 'latitude': 34.123,
                 'longitude': -118.123,
                 'depth': 30.1,
                 'magnitude': 7.8,
                 'locstring': 'Somewhere in California'}
    try:
        tdir = tempfile.mkdtemp()
        dbfile = pathlib.Path(tdir) / 'test.db'
        # dbfile.touch()
        dburl = dbfile.as_uri().replace('file:', 'sqlite:/')
        os.environ['DB_URL'] = dburl
        insert_event(eventdict)
        with mock.patch('boto3.client', return_value=MockS3Client()) as _:
            run_events()

        # assert that the event is now in the running table
        session = get_session(dburl)
        assert session.query(Running).count() == 1
        session.close()

        # test the cleaning functionality when events have run
        # over without finishing
        time.sleep(4)
        mock_config = {'queue': {'max_process_time': 3,
                                 'email': {'sender': 'pager@usgs.gov',
                                           'error_emails': ['foo@outlook.com']}
                                 },
                       }
        mock1 = 'sm_queue.queue.get_config'
        mock2 = 'boto3.client'
        with mock.patch(mock1, return_value=mock_config) as _, \
                mock.patch(mock2, return_value=MockEmailClient()) as _:
            clean_running()

        # assert that the event is now gone from the running table
        session = get_session(dburl)
        assert session.query(Running).count() == 0
        session.close()

        # TODO: test cleaning functionality when event has successfully
        # finished running
        # make sure to test lastrun setting in Event table
        insert_event(eventdict)
        with mock.patch('boto3.client', return_value=MockS3Client()) as _:
            run_events()

        # now find the running event, and set it to be successful
        session = get_session(dburl)
        running = session.query(Running).first()
        running.success = True
        session.commit()

        # now clean up the successful run
        with mock.patch(mock1, return_value=mock_config) as _, \
                mock.patch(mock2, return_value=MockEmailClient()) as _:
            clean_running()

        assert session.query(Running).count() == 0
        assert session.query(Event).first().lastrun > datetime(1900, 1, 1)
        session.close()

    except Exception as e:
        msg = f'Something unexpected happened! {str(e)}'
        raise AssertionError(msg)
    finally:
        shutil.rmtree(tdir)


def test_queue_objects():
    session = get_session()  # in-memory sqlite session
    eventdict = {'eventid': 'us2020abcd',
                 'netid': 'us',
                 'ids': ['ci12345678'],
                 'time': datetime.utcnow(),
                 'latitude': 34.123,
                 'longitude': -118.123,
                 'depth': 30.1,
                 'magnitude': 7.8,
                 'locstring': 'Somewhere in California'}
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
    queue1 = Queued(event_id=event.id, run_time=datetime.utcnow())
    session.add(queue1)
    session.commit()
    queue2 = session.query(Queued).filter_by(event_id=event.id).first()
    assert queue2.run_time == queue1.run_time

    running1 = Running(queued_id=queue1.id,
                       start_time=datetime.utcnow(),
                       success=False)
    session.add(running1)
    session.commit()
    running2 = session.query(Running).filter_by(queued_id=queue1.id).first()
    assert running2.start_time == running1.start_time
    assert running2.success is False
    session.close()


if __name__ == '__main__':
    test_queue_objects()
    test_config()
    test_run_events()
    test_insert()
