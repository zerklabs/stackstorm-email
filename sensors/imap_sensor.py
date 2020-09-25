import hashlib
import base64
import imaplib
import email

import six
import eventlet
import easyimap
import ssl
from ssl import Purpose
from flanker import mime

from st2reactor.sensor.base import PollingSensor

__all__ = [
    'IMAPSensor'
]

eventlet.monkey_patch(
    os=True,
    select=True,
    socket=True,
    thread=True,
    time=True)

DEFAULT_DOWNLOAD_ATTACHMENTS = False
DEFAULT_MAX_ATTACHMENT_SIZE = 1024
DEFAULT_ATTACHMENT_DATASTORE_TTL = 1800


class IMAPSensor(PollingSensor):
    def __init__(self, sensor_service, config=None, poll_interval=30):
        super(IMAPSensor, self).__init__(sensor_service=sensor_service,
                                         config=config,
                                         poll_interval=poll_interval)

        self._trigger = 'email.imap.message'
        self._logger = self._sensor_service.get_logger(__name__)

        self._max_attachment_size = self._config.get('max_attachment_size',
                                                     DEFAULT_MAX_ATTACHMENT_SIZE)
        self._attachment_datastore_ttl = self._config.get('attachment_datastore_ttl',
                                                          DEFAULT_MAX_ATTACHMENT_SIZE)
        self._accounts = {}

    def setup(self):
        self._logger.debug('[IMAPSensor]: entering setup')

    def poll(self):
        self._logger.debug('[IMAPSensor]: entering poll')

        if 'imap_accounts' in self._config:
            self._parse_accounts(self._config['imap_accounts'])

        for name, values in self._accounts.items():
            mailbox = values['connection']
            download_attachments = values['download_attachments']
            mailbox_metadata = values['mailbox_metadata']

            self._poll_for_unread_messages(name=name, mailbox=mailbox,
                                           download_attachments=download_attachments,
                                           mailbox_metadata=mailbox_metadata)
            
            mailbox.close()
            mailbox.logout()

    def cleanup(self):
        self._logger.debug('[IMAPSensor]: entering cleanup')

        for name, values in self._accounts.items():
            mailbox = values['connection']
            self._logger.debug('[IMAPSensor]: Disconnecting from {0}'.format(name))
            mailbox.quit()

    def add_trigger(self, trigger):
        pass

    def update_trigger(self, trigger):
        pass

    def remove_trigger(self, trigger):
        pass

    def _parse_accounts(self, accounts):
        for config in accounts:
            mailbox = config.get('name', None)
            server = config.get('server', 'localhost')
            port = config.get('port', 993)
            user = config.get('username', None)
            password = config.get('password', None)
            folder = config.get('folder', 'INBOX')
            use_tls = config.get('use_tls', True)
            download_attachments = config.get('download_attachments', DEFAULT_DOWNLOAD_ATTACHMENTS)

            if not user or not password:
                self._logger.debug("""[IMAPSensor]: Missing
                    username/password for {0}""".format(mailbox))
                continue

            if not server:
                self._logger.debug("""[IMAPSensor]: Missing server
                    for {0}""".format(mailbox))
                continue

            try:
                if use_tls:
                    connection = imaplib.IMAP4_SSL(host=server, port=port, ssl_context=ssl.create_default_context(Purpose.SERVER_AUTH))
                else:
                    connection = imaplib.IMAP4(host=server, port=port)
            except Exception as e:
                message = 'Failed to establish connection to server "%s:%d": %s' % (server, port, str(e))
                raise Exception(message)

            try:
                connection.login(user, password)
            except Exception as e:
                message = 'Failed to login to server "{0}:{1}" as {2}: {3}'.format(server, port, user, str(e))
                raise Exception(message)

            typ, dat = connection.select(folder)
            self._logger.debug("[IMAPSensor]: IMAP select response: {0}".format(typ))
            item = {
                'connection': connection,
                'download_attachments': download_attachments,
                'mailbox_metadata': {
                    'server': server,
                    'port': port,
                    'user': user,
                    'folder': folder,
                    'use_tls': use_tls
                }
            }
            self._accounts[mailbox] = item

    def _poll_for_unread_messages(self, name, mailbox, mailbox_metadata,
                                  download_attachments=False):
        self._logger.debug('[IMAPSensor]: polling mailbox {0}'.format(name))

        status, response = mailbox.search(None, '(UNSEEN)')
        unread_msg_nums = response[0].split()

        # Print the count of all unread messages
        self._logger.info('[IMAPSensor]: Found {0} unread messages'.format(len(unread_msg_nums)))

        da = []
        for e_id in unread_msg_nums:
            _, response = mailbox.fetch(e_id, '(UID BODY[TEXT])')
            item = {
                'uid': e_id,
                'raw': response[0][1]
            }

            da.append(item)

        for message in da:
            try:
                self._logger.debug('[IMAPSensor]: Raw message {0}'.format(message['raw']))
                msg = email.message_from_bytes(message['raw'])
                self._logger.debug('[IMAPSensor]: Parsed message type {0}'.format(type(msg)))
                self._logger.debug('[IMAPSensor]: Parsed message dir {0}'.format(dir(msg)))
                self._logger.debug('[IMAPSensor]: Parsed message vars {0}'.format(vars(msg)))
                self._logger.debug('[IMAPSensor]: Parsed message {0}'.format(msg))
                self._process_message_std(message=msg, mailbox=mailbox, download_attachments=download_attachments, mailbox_metadata=mailbox_metadata)
                mailbox.store(message['uid'], '+FLAGS', '\Seen')
            except Exception as e:
                exmessage = 'Failed to read message {0}'.format(str(e))
                self._logger.error('[IMAPSensor] {0}'.format(exmessage))

        # Mark them as seen
        # for e_id in unread_msg_nums:
        #     try:
        #         mailbox.store(e_id, '+FLAGS', '\Seen')
        #     except Exception as e:
        #         message = 'Failed to process message {0}: {1}'.format(e_id, str(e))
        #         self._logger.error('[IMAPSensor] {0}'.format(message))


        # messages = mailbox.unseen()

        # self._logger.debug('[IMAPSensor]: Processing {0} new messages'.format(len(messages)))
        # for message in messages:
        #     self._process_message(uid=message.uid, mailbox=mailbox,
        #                           download_attachments=download_attachments,
        #                           mailbox_metadata=mailbox_metadata)

    def _process_message_std(self, message, mailbox, mailbox_metadata,
                             download_attachments=DEFAULT_DOWNLOAD_ATTACHMENTS):
        for part in message.walk():
            self._logger.debug('[IMAPSensor] walking message sub-part type: {0}'.format(type(part)))
            self._logger.debug('[IMAPSensor] walking message sub-part content type: {0}'.format(part.get_content_type()))
            try:
                body = part.get_body()
                self._logger.debug('[IMAPSensor] sub-part body: {0}'.format(body))
                self._logger.debug('[IMAPSensor] sub-part body type: {0}'.format(type(body)))
                self._logger.debug('[IMAPSensor] sub-part body vars: {0}'.format(vars(body)))
                self._logger.debug('[IMAPSensor] sub-part body dir: {0}'.format(dir(body)))
            except Exception as e:
                exmessage = 'Failed to get part body {0}'.format(str(e))
                raise Exception(exmessage)

    def _process_message(self, uid, mailbox, mailbox_metadata,
                         download_attachments=DEFAULT_DOWNLOAD_ATTACHMENTS):
        self._logger.debug('[IMAPSensor]: Processing message with uid {0}'.format(uid))

        # if we receive the TypeError "can't concat int into bytes", cast uid
        try:
            message = mailbox.mail(uid, include_raw=True)
        except TypeError:
            message = mailbox.mail(str(uid), include_raw=True)

        mime_msg = mime.from_string(message.raw)

        try:
            body = message.body
        except Exception as e:
            body = ''
            self._logger.debug('[IMAPSensor]: exception raised from easyimap {0}'.format(str(e)))

        sent_from = message.from_addr
        sent_to = message.to
        subject = message.title
        date = message.date
        message_id = message.message_id
        headers = mime_msg.headers.items()
        has_attachments = bool(message.attachments)

        # Flatten the headers so they can be unpickled
        headers = self._flattern_headers(headers=headers)

        payload = {
            'uid': uid,
            'from': sent_from,
            'to': sent_to,
            'headers': headers,
            'date': date,
            'subject': subject,
            'message_id': message_id,
            'body': body,
            'has_attachments': has_attachments,
            'attachments': [],
            'mailbox_metadata': mailbox_metadata
        }

        if has_attachments and download_attachments:
            self._logger.debug('[IMAPSensor]: Downloading attachments for message {}'.format(uid))
            result = self._download_and_store_message_attachments(message=message)
            payload['attachments'] = result

        self._sensor_service.dispatch(trigger=self._trigger, payload=payload)

    def _download_and_store_message_attachments(self, message):
        """
        Method which downloads the provided message attachments and stores them in a datasatore.

        :rtype: ``list`` of ``dict``
        """
        attachments = message.attachments

        result = []
        for (file_name, content, content_type) in attachments:
            attachment_size = len(content)

            if len(content) > self._max_attachment_size:
                self._logger.debug(('[IMAPSensor]: Skipping attachment "{}" since its bigger '
                                    'than maximum allowed size ({})'.format(file_name,
                                                                            attachment_size)))
                continue

            datastore_key = self._get_attachment_datastore_key(message=message,
                                                               file_name=file_name)

            # Store attachment in the datastore
            if content_type == 'text/plain':
                value = content
            else:
                value = base64.b64encode(content)

            self._sensor_service.set_value(name=datastore_key, value=value,
                                           ttl=self._attachment_datastore_ttl,
                                           local=False)
            item = {
                'file_name': file_name,
                'content_type': content_type,
                'datastore_key': datastore_key
            }
            result.append(item)

        return result

    def _get_attachment_datastore_key(self, message, file_name):
        key = '%s-%s' % (message.uid, file_name)
        key = 'attachments-%s' % (hashlib.md5(key).hexdigest())
        return key

    def _flattern_headers(self, headers):
        # Flattern headers and make sure they only contain simple types so they
        # can be serialized in a trigger
        result = []

        for pair in headers:
            name = pair[0]
            value = pair[1]

            if not isinstance(value, six.string_types):
                value = str(value)

            result.append([name, value])

        return result
