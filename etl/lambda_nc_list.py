"""
Lambda handler: serve a private NC list for a given date.
Deploy with env vars S3_BUCKET and REGION.

Event: { "date": "2026-09-15" }  (API Gateway or direct invocation)
"""

import json, os, boto3
from botocore.exceptions import ClientError

S3_BUCKET = os.environ.get('S3_BUCKET', 'lenity-stratus-state')
REGION    = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
PREFIX    = 'stratus/dashboard/nc'

CORS = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'GET,OPTIONS',
}


def handler(event, context):
    # Support both direct invocation and API Gateway proxy
    if isinstance(event.get('body'), str):
        try:
            body = json.loads(event['body'])
        except Exception:
            body = {}
    else:
        body = event

    date_str = (body.get('date') or
                event.get('queryStringParameters', {}).get('date', ''))

    if not date_str or not _valid_date(date_str):
        return _resp(400, {'error': 'Missing or invalid date parameter (YYYY-MM-DD)'})

    key = f'{PREFIX}/nc_{date_str}.json'
    s3  = boto3.client('s3', region_name=REGION)

    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        nc_list = json.loads(obj['Body'].read())
        return _resp(200, nc_list)
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            return _resp(404, {'error': f'No NC list for {date_str}'})
        raise


def _valid_date(s):
    import re
    return bool(re.match(r'^\d{4}-\d{2}-\d{2}$', s))


def _resp(status, body):
    return {
        'statusCode': status,
        'headers': {**CORS, 'Content-Type': 'application/json'},
        'body': json.dumps(body),
    }
