#!/usr/bin/python
#
# (c) 2012/2014 E.M. van Nuil / Oblivion b.v.
#
# makesnapshots.py version 3.3
#
# Changelog
# version 1:   Initial version
# version 1.1: Added description and region
# version 1.2: Added extra error handeling and logging
# version 1.3: Added SNS email functionality for succes and error reporting
# version 1.3.1: Fixed the SNS and IAM problem
# version 1.4: Moved all settings to config file
# version 1.5: Select volumes for snapshotting depending on Tag and not from config file
# version 1.5.1: Added proxyHost and proxyPort to config and connect
# version 1.6: Public release
# version 2.0: Added daily, weekly and montly retention
# version 3.0: Rewrote deleting functions, changed description
# version 3.1: Fix a bug with the deletelist and added a pause in the volume loop
# version 3.2: Tags of the volume are placed on the new snapshot
# version 3.3: Merged IAM role addidtion from Github

import argparse
import datetime
import logging
import sys
import time

from boto.ec2.connection import EC2Connection
from boto.ec2.regioninfo import RegionInfo
import boto.sns

from config import config


parser = argparse.ArgumentParser(description='''
    For a specified period, take snapshots of all the EBS volumes, which are
    tagged properly (see the config file).
    ''')
parser.add_argument('period', metavar='P', type=str,
                    choices=('hour', 'four_hours', 'day', 'week', 'month'),
                    help='Period of the snapshots')
args = parser.parse_args()

# Frequency label, used for logging and the snapshots' descriptions
frequency = {
    'hour': 'hourly',
    'four_hours': 'four-hourly',
    'day': 'daily',
    'week': 'weekly',
    'month': 'monthly'
}[args.period]

# Messages to return via SNS
sns_msg = sns_err_msg = ""

# Counters
total_created = 0
total_deleted = 0
count_total = 0
count_success = 0
count_errors = 0

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s (%(levelname)s): %(message)s')

fh = logging.FileHandler(config['log_file'])
fh.setFormatter(formatter)
sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(sh)

msg = 'Started taking %s snapshots at %s' % (frequency, datetime.datetime.utcnow().strftime('%d-%m-%Y %H:%M:%S'))
logger.info(msg)
sns_msg += msg + "\n"

# Get settings from config.py
aws_access_key = config['aws_access_key']
aws_secret_key = config['aws_secret_key']
ec2_region_name = config['ec2_region_name']
ec2_region_endpoint = config['ec2_region_endpoint']
sns_arn = config.get('arn')
proxyHost = config.get('proxyHost')
proxyPort = config.get('proxyPort')

region = RegionInfo(name=ec2_region_name, endpoint=ec2_region_endpoint)

# Connect to AWS using the credentials provided above or in environment vars or using IAM role.
logger.info('Connecting to AWS')
if proxyHost:
    if aws_access_key:
        conn = EC2Connection(aws_access_key, aws_secret_key, region=region, proxy=proxyHost, proxy_port=proxyPort)
    else:
        conn = EC2Connection(region=region, proxy=proxyHost, proxy_port=proxyPort)
else:
    if aws_access_key:
        conn = EC2Connection(aws_access_key, aws_secret_key, region=region)
    else:
        conn = EC2Connection(region=region)

# Connect to SNS, if ARN was specified in the config
if sns_arn:
    logger.info('Connecting to SNS')
    if proxyHost:
        if aws_access_key:
            sns = boto.sns.connect_to_region(ec2_region_name, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, proxy=proxyHost, proxy_port=proxyPort)
        else:
            sns = boto.sns.connect_to_region(ec2_region_name, proxy=proxyHost, proxy_port=proxyPort)
    else:
        if aws_access_key:
            sns = boto.sns.connect_to_region(ec2_region_name, aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)
        else:
            sns = boto.sns.connect_to_region(ec2_region_name)

# Helper methods to get the tags from a volume and add them to a snapshot
def get_resource_tags(resource_id):
    resource_tags = {}
    if resource_id:
        tags = conn.get_all_tags({ 'resource-id': resource_id })
        for tag in tags:
            # Tags starting with 'aws:' are reserved for internal use
            if not tag.name.startswith('aws:'):
                resource_tags[tag.name] = tag.value
    return resource_tags

def set_resource_tags(resource, tags):
    for tag_key, tag_value in tags.iteritems():
        if tag_key not in resource.tags or resource.tags[tag_key] != tag_value:
            resource.add_tag(tag_key, tag_value)

# Get all the volumes that match the tag criteria
logger.info('Finding volumes that match the requested tag ({ "tag:%(tag_name)s": "%(tag_value)s" })' % config)
try:
    vols = conn.get_all_volumes(filters={ 'tag:' + config['tag_name']: config['tag_value'] })
    count_total = len(vols)
except Exception, e:
    vols = []
    msg = u'Volumes could not be listed: ' + unicode(e)
    logger.error(msg)
    sns_err_msg += msg

for vol in vols:
    try:
        tags_volume = get_resource_tags(vol.id)
        description = '%(frequency)s snapshot for %(vol_id)s taken by the snapshot script at %(timestamp)s' % {
            'frequency': frequency,
            'vol_id': vol.id,
            'timestamp': datetime.datetime.utcnow().strftime('%d-%m-%Y %H:%M:%S')
        }
        current_snap = vol.create_snapshot(description)
        set_resource_tags(current_snap, tags_volume)
        logger.info('Snapshot created with description: "%s" and tags: %s' % (description, tags_volume))
        total_created += 1

        # Create a list of all snapshots for a specified period
        snapshots = vol.snapshots()

        # Filter the relevant snapshots (description matching the frequency)
        relevant_snaps = [snap for snap in snapshots if (
                snap.description.startswith(frequency) and
                'taken by the snapshot script' in snap.description
            )
        ]

        # Sort the list by the snapshot date, from the oldest to the newest
        relevant_snaps.sort(key=lambda snap: snap.start_time)

        keep_count = config.get('keep_' + args.period)

        if keep_count is None:
            logger.info('No retention policy found in the config for %s, skipping' % 'keep_' + args.period)
            continue

        # Cut the list to only include the outdated snapshots
        delta = len(relevant_snaps) - keep_count
        deletelist = relevant_snaps[:delta] if delta > 0 else []

        # Delete the snapshots
        for snap in deletelist:
            logger.info('Deleting snapshot: %s' % snap.description)
            snap.delete()
            total_deleted += 1

        # Sleep after processing each volume
        time.sleep(3)
    except Exception, e:
        msg = 'Error in processing volume with id: ' + vol.id
        logger.error(msg)
        logger.error(e)
        sns_err_msg += msg + '\n'
        count_errors += 1
    else:
        count_success += 1

result = 'Finished taking snapshots at %(timestamp)s with %(count_success)s snapshots out of %(count_total)s possible.\n' % {
    'timestamp': datetime.datetime.utcnow().strftime('%d-%m-%Y %H:%M:%S'),
    'count_success': count_success,
    'count_total': count_total
}
result += "Total snapshots created: %d\n" % total_created
result += "Total snapshots deleted: %d\n" % total_deleted
result += "Total snapshots errors: %d\n" % count_errors

sns_msg += result

# Not finding any volumes is considered an error
if not vols:
    msg = u'No volumes found'
    logger.error(msg)
    sns_err_msg += msg

# SNS reporting
if sns_arn:
    if sns_err_msg:
        sns_err_msg = 'Some of the volumes could not be processed. See the logs for more detailed info.\n\n' + sns_err_msg
        sns.publish(sns_arn, sns_err_msg, 'Error with AWS Snapshot')
    sns.publish(sns_arn, sns_msg, 'Finished taking AWS snapshots')

logger.info(result)

if sns_err_msg:
    sys.exit(1)
