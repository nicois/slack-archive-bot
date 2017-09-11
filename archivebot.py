import os
import time
import shlex
import logging
import argparse
import sqlite3

from slackclient import SlackClient
from websocket import WebSocketConnectionClosedException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# Connects to the previously created SQL database
# TODO: lock on slack.sqlite to ensure only one instance is running

conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), 'slack.sqlite'))
conn.row_factory = sqlite3.Row


try:
    conn.execute('ALTER TABLE messages ADD COLUMN thread_timestamp TEXT')
except sqlite3.OperationalError:
    pass  # this is ok. It just means the column already exists.
conn.execute('create table if not exists messages (message text, user text, channel text, timestamp text, thread_timestamp text, UNIQUE(channel, timestamp) ON CONFLICT REPLACE)')
conn.execute('create table if not exists users (name text, id text, avatar text, UNIQUE(id) ON CONFLICT REPLACE)')
conn.execute('create table if not exists channels (name text, id text, UNIQUE(id) ON CONFLICT REPLACE)')
# Keep track of which people have used the service, and when
conn.execute('create table if not exists last_query (channel text, timestamp INT, UNIQUE(channel) ON CONFLICT REPLACE)')

# This token is given when the bot is started in terminal
slack_token = os.environ['SLACK_API_TOKEN']

# Makes bot user active on Slack
# NOTE: terminal must be running for the bot to continue
sc = SlackClient(slack_token)

known_channels = set(record[0] for record in conn.execute("SELECT channel, count(*) FROM messages GROUP BY channel"))


# Double naming for better search functionality
# Keys are both the name and unique ID where needed
ENV = {
    'user_id': {},
    'id_user': {},
    'channel_id': {},
    'id_channel': {},
    'subscribed_channels': set()
}


def render_timestamp(ts):
    return '<!date^%s^{date_short} {time_secs}|date>' % get_timestamp(ts)


# Uses slack API to get most recent user list
# Necessary for User ID correlation
def update_users(conn):
    info = sc.api_call('users.list')
    ENV['user_id'] = dict([(m['name'], m['id']) for m in info['members']])
    ENV['id_user'] = dict([(m['id'], m['name']) for m in info['members']])

    args = []
    for m in info['members']:
        args.append((
            m['name'],
            m['id'],
            m['profile'].get('image_72', 'https://secure.gravatar.com/avatar/c3a07fba0c4787b0ef1d417838eae9c5.jpg?s=32&d=https%3A%2F%2Ffst.slack-edge.com%2F66f9%2Fimg%2Favatars%2Fava_0024-32.png')
        ))
    conn.executemany('INSERT INTO users(name, id, avatar) VALUES(?,?,?)', args)
    logger.info('Users updated')


def get_user_name(uid):
    if uid not in ENV['id_user']:
        update_users()
    return ENV['id_user'].get(uid, None)


def get_user_id(name):
    if name not in ENV['user_id']:
        update_users()
    return ENV['user_id'].get(name, None)


def get_timestamp(ts):
    return round(float(ts))


def update_groups(conn):
    info = sc.api_call('groups.list', exclude_members=True)
    for m in info['groups']:
        ENV['channel_id'][m['name']] = m['id']
        ENV['id_channel'][m['id']] = m['name']
        ENV['subscribed_channels'].add(m['id'])
    logger.info('Groups updated')


def save_channels(conn):
    args = sorted(ENV['channel_id'].items())
    conn.executemany('INSERT INTO channels(name, id) VALUES(?,?)', args)
    logger.info('Saved groups and channels')


def update_channels(conn):
    info = sc.api_call('channels.list', exclude_members=True)
    for m in info['channels']:
        ENV['channel_id'][m['name']] = m['id']
        ENV['id_channel'][m['id']] = m['name']
        if m['is_member']:
            ENV['subscribed_channels'].add(m['id'])
    logger.info('Channels updated')


def get_channel_name(uid):
    if uid not in ENV['id_channel']:
        update_channels()
    return ENV['id_channel'].get(uid, None)


def get_channel_id(name):
    if name not in ENV['channel_id']:
        update_channels()
    return ENV['channel_id'].get(name, None)


def send_message(message, channel):
    sc.api_call(
      'chat.postMessage',
      channel=channel,
      text=message
    )


def time_to_show_help(event):
    """
    Has it been a while since we showed help,
    or have they asked for it explicitly?
    """
    channel = event['channel']
    sql = f"SELECT timestamp FROM last_query WHERE channel=?"
    result = list(conn.execute(sql, (channel,)))
    logger.warning(repr(result))
    now = int(time.time())
    # if it's been over a month since using us, show the help again
    result = (len(result) == 0 or result[0][0] is None or (now - result[0][0]) > (3600 * 24 * 30))
    sql = "INSERT INTO last_query (channel, timestamp) VALUES (?, ?)"
    conn.execute(sql, (channel, now))

    if event['text'].lower().strip() == "help":
        result = True

    return result


def handle_query(event):
    """
    Usage:

        <query> from:<user> in:<channel> sort:asc|desc limit:<number>

        query: The text to search for. Use quotes around multi-word strings
        user: If you want to limit the search to one user, the username.
        channel: If you want to limit the search to one channel, the channel name.
        sort: Either asc if you want to search starting with the oldest messages,
            or desc if you want to start from the newest. Default asc.
        limit: The number of responses to return. Default 10.

        e.g. "smart planner" limit:3
        would search all channels for the term "smart planner" (without the quotes)
        and show the 3 oldest entries.
    """

    parser = argparse.ArgumentParser(description=handle_query.__doc__)
    parser.add_argument("query", help="Text to search for")
    parser.add_argument("--limit", help="Show this many results",
                        type=int, default=10)
    parser.add_argument("--newest", help="Show the newest results first",
                        action="store_true")
    parser.add_argument("--sender", help="Only show messages from this person")
    parser.add_argument("--channel", help="Only show messages in this channel")

    try:
        text = []

        if time_to_show_help(event):
            send_message(handle_query.__doc__, event['channel'])
            return

        params = shlex.split(event['text'].lower())
        logger.info(f'Processing query: {params}')
        if params == ["stats"]:
            send_stats(channel=event['channel'])
            return

        args = parser.parse_args(params)
        sort = "desc" if args.newest else "asc"
        where = [f'messages.message LIKE "%{args.query}%"']
        if args.sender:
            user = get_user_id(args.sender.replace('@', '').strip())
            where.append(f'user="{user}"')
        if args.channel:
            where.append(f'channel="{args.channel}"')

        query = '''
SELECT messages.*,
       tm.message AS thread_title
FROM messages
LEFT OUTER JOIN (SELECT timestamp, message, user, channel FROM messages) tm
    ON (messages.thread_timestamp = tm.timestamp AND
        messages.channel = tm.channel)
WHERE {where}
ORDER BY COALESCE(tm.timestamp, messages.timestamp) {sort}, messages.timestamp {sort}
LIMIT {limit}
'''.format(where=" AND ".join(where), sort=sort, limit=args.limit)
        logger.info(query)

        res = list(conn.execute(query))

        if res:
            send_message('\n\n'.join(
                [format_response(line)
                 for line in res]
            ), event['channel'])
        else:
            send_message('No results found', event['channel'])
    except ValueError as e:
        logger.exception('During query')
        send_message(str(e), event['channel'])


def send_stats(channel):
    sql = """
SELECT COUNT(*) as n,
       MIN(timestamp) as earliest,
       MAX(timestamp) as latest
FROM messages"""
    (n, earliest, latest), = conn.execute(sql)

    send_message(f'{n} messages from {render_timestamp(earliest)} '
                 f'to {render_timestamp(latest)}',
                 channel)


def handle_message(conn, event):
    if 'text' not in event:
        return
    if 'username' in event and event['username'] == 'bot':
        return

    try:
        logger.debug(event)
    except:
        logger.debug('*' * 20)

    # If it's a DM, treat it as a search query
    channel = event['channel']
    if channel[0] == 'D':
        handle_query(event)
    elif 'user' not in event:
        logger.debug('No valid user. Previous event not saved')
    else:  # Otherwise save the message to the archive.
        if channel not in known_channels:
            logger.debug("{} is a new channel. Stand by while syncing its history".format(channel))
            known_channels.add(channel)
            sync_channel(channel_id=channel, conn=conn)

        conn.execute('INSERT INTO messages VALUES(?, ?, ?, ?, ?)',
                     (event['text'],
                      event['user'],
                      event['channel'],
                      event['ts'],
                      event.get('thread_ts')))
        logger.debug('--------------------------')


def format_response(line):
    # message, user, timestamp, channel, thread_timestamp, thread_title):
    message = '\n'.join(map(lambda s: '> %s' % s, line['message'].split('\n')))  # add > before each line
    username = get_user_name(line['user'])
    timestamp = get_timestamp(line['timestamp'])
    if line['thread_timestamp'] is not None:
        thread_timestamp = get_timestamp(line['thread_timestamp'])
        return '*<@%s> <#%s> <!date^%s^{date_short} {time_secs}|date>* <!date^%s^{date_short} {time_secs}|date>*\n %s %s)' % (username, line['channel'], timestamp, thread_timestamp, line['thread_title'], message)
    else:
        return '*<@%s> <#%s> <!date^%s^{date_short} {time_secs}|date>*\n%s)' % (username, line['channel'], timestamp, message)


def update_channel_history(conn):
    """
    For each channel we have previously received, check if there are any later messages
    which we missed
    """
    channels_map = dict(record for record in conn.execute("SELECT channel, MAX(timestamp) as latest_timestamp FROM messages GROUP BY channel"))
    for channel_id, last_seen in channels_map.items():
        if channel_id is not None:
            # FIXME: if channel is archived or deleted,
            # this will raise an exception - which is OK.
            # But during development/testing, we'll keep it failing
            # to catch issues with the sync
            sync_channel(channel_id=channel_id, oldest=last_seen, conn=conn)


def sync_channel(conn, channel_id, oldest=None):
    """
    Keeps reading channel history until we have caught up.
    """
    latest = None
    logger.info("Checking channel {}".format(channel_id))
    has_more = True
    total = 0
    api_name = 'groups.history' if channel_id.startswith('G') else 'channels.history'
    while has_more:
        kw = dict()
        if oldest is not None:
            kw['oldest'] = oldest
        if latest is not None:
            kw['latest'] = latest
        result = sc.api_call(api_name,
                             channel=channel_id,
                             **kw)
        if not result['ok']:
            raise Exception('{}: {}'.format(channel_id, result['error']))

        timestamps = set()
        for message in result['messages']:
            message['channel'] = channel_id
            timestamps.add(float(message['ts']))
            handle_message(conn=conn, event=message)
        total += len(result['messages'])

        logger.info("Processed {} messages so far".format(total))
        if len(timestamps) > 0:
            latest = min(timestamps)
        has_more = result['has_more']


# Loop
if sc.rtm_connect():
    with conn:
        update_users(conn)
        update_channels(conn)
        update_groups(conn)
        update_channel_history(conn)
        save_channels(conn)
        logger.info('Archive bot online. Messages will now be recorded...')
    while True:
        try:
            for event in sc.rtm_read():
                if event['type'] == 'message':
                    with conn:
                        handle_message(conn=conn, event=event)
        except WebSocketConnectionClosedException:
            sc.rtm_connect()
        except:
            logger.exception("In main RTC loop")
        time.sleep(1)
else:
    logger.error('Connection Failed, invalid token?')
