"""Utility functions for manipulating datetime information."""
import datetime
import logging

log = logging.getLogger(__name__)


def add_microseconds(timestamp, microseconds):
    """
    Add given microseconds to a timestamp.

    Input and output are timestamps as ISO format strings.  Microseconds can be negative.
    """
    # First try to parse the timestamp string and do simple math, to avoid
    # the high cost of using strptime to parse in most cases.
    timestamp_base, _period, microsec_base = timestamp.partition('.')
    if not microsec_base:
        microsec_base = '0'
        timestamp = '{datetime}.000000'.format(datetime=timestamp)
    microsec_int = int(microsec_base) + microseconds
    if microsec_int >= 0 and microsec_int < 1000000:
        return "{}.{}".format(timestamp_base, str(microsec_int).zfill(6))

    # If there's a carry, then just use the datetime library.
    parsed_timestamp = datetime.datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%f')
    newtimestamp = (parsed_timestamp + datetime.timedelta(microseconds=microseconds)).isoformat()
    if '.' not in newtimestamp:
        newtimestamp = '{datetime}.000000'.format(datetime=newtimestamp)
    log.debug("Adding %d microseconds to timestamp %s yields %s", microseconds, timestamp, newtimestamp)
    return newtimestamp