"""

  This code is released under the GNU Affero General Public License.
  
  OpenEnergyMonitor project:
  http://openenergymonitor.org

"""

import urllib2
import httplib
import time
import logging
import json
import threading
import Queue

import emonhub_buffer as ehb
  
"""class EmonHubDispatcher

Stores server parameters and buffers the data between two HTTP requests

This class is meant to be inherited by subclasses specific to their 
destination server.

"""


class EmonHubDispatcher(threading.Thread):

    def __init__(self, dispatcherName, queue, bufferMethod="memory", bufferSize=1000, **kwargs):
        """Create a server data buffer initialized with server settings."""

        # Initialize logger
        self._log = logging.getLogger("EmonHub")

        # Initialise thread
        threading.Thread.__init__(self)

        # Initialise settings
        self.name = dispatcherName
        self.init_settings = {}
        self._defaults = {'pause': 0, 'interval': 0, 'maxItemsPerPost': 1}
        self._settings = {}
        self._queue = queue

        # This line will stop the default values printing to logfile at start-up
        # unless they have been overwritten by emonhub.conf entries
        # comment out if diagnosing a startup value issue
        self._settings.update(self._defaults)

        # Initialize interval timer's "started at" timestamp
        self._interval_timestamp = 0

        # Create underlying buffer implementation
        self.buffer = ehb.getBuffer(bufferMethod)(dispatcherName, bufferSize, **kwargs)

        # set an absolute upper limit for number of items to process per post
        # number of items posted is the lower of this item limit, buffersize, or the
        # maxItemsPerPost, as set in dispatcher settings or by the default value.
        self._item_limit = bufferSize
        
        self._log.info("Set up dispatcher '%s' (buffer: %s | size: %s)"
                       % (dispatcherName, bufferMethod, bufferSize))

        # Initialise a thread and start the dispatcher
        self.stop = False
        self.start()
        
    def set(self, **kwargs):
        """Update settings.
        
        **kwargs (dict): runtime settings to be modified.
        
        url (string): eg: 'http://localhost/emoncms' or 'http://emoncms.org' (trailing slash optional)
        apikey (string): API key with write access
        pause (string): pause status
            'pause' = i/I/in/In/IN to pause the input only, no add to buffer but flush still functional
            'pause' = o/O/out/Out/OUT to pause output only, no flush but data can accumulate in buffer
            'pause' = t/T/true/True/TRUE nothing gets posted to buffer or sent by url (buffer retained)
            'pause' = anything else, commented out or omitted then dispatcher is fully operational
        
        """

        for key, setting in self._defaults.iteritems():
            if not key in kwargs.keys():
                setting = self._defaults[key]
            else:
                setting = kwargs[key]
            if key in self._settings and self._settings[key] == setting:
                pass
            else:
                self._settings[key] = setting
                self._log.debug("Setting " + self.name + " " + key + ": " + str(setting))

        # apply any changes to non-default settings (eg apikey)
        for key, setting in kwargs.iteritems():
            if key in self._settings and setting != self._settings[key]:
                self._settings[key] = setting

    def add(self, data):
        """Append data to buffer.

        data (list): node and values (eg: '[node,val1,val2,...]')

        """

        self._log.debug(str(data[-1]) + " Append to '" + self.name +
                        "' buffer => time: " + str(data[0])
                        + ", data: " + str(data[1:-1])
                        # TODO "ref" temporarily left on end of data string for info
                        + ", ref: " + str(data[-1]))
        # TODO "ref" removed from end of data string here so not sent to emoncms
        data = data[:-1]

        # databuffer is of format:
        # [[timestamp, nodeid, datavalues][timestamp, nodeid, datavalues]]
        # [[1399980731, 10, 150, 3450 ...]]
        self.buffer.storeItem(data)

    def run(self):
        """
        Run the dispatcher thread.
        Any regularly performed tasks actioned here along with flushing the buffer

        """
        while not self.stop:
            # If there are frames in the queue
            while not self._queue.empty():
                # Add each frame to the buffer
                frame = self._queue.get()
                self.add(frame)
            # Don't loop to fast
            time.sleep(0.1)
            # Action dispatcher tasks
            self.action()

    def action(self):
        """

        :return:
        """

        # pause output if 'pause' set to true or to pause output only
        if 'pause' in self._settings and self._settings['pause'] in \
                ['o', 'O', 'out', 'Out', 'OUT', 't', 'T', 'true', 'True', 'TRUE']:
            return

        # If an interval is set, check if that time has passed since last post
        if int(self._settings['interval']) and time.time() - self._interval_timestamp < int(self._settings['interval']):
            return
        else:
            # Then attempt to flush the buffer
            self.flush()

    def flush(self):
        """Send oldest data in buffer, if any."""
        
        # Buffer management
        # If data buffer not empty, send a set of values
        if self.buffer.hasItems():
            max_items = int(self._settings['maxItemsPerPost'])
            if max_items > self._item_limit:
                max_items = self._item_limit
            elif max_items <= 0:
                return

            databuffer = self.buffer.retrieveItems(max_items)
            retrievedlength = len(databuffer)
            if self._process_post(databuffer):
                # In case of success, delete sample set from buffer
                self.buffer.discardLastRetrievedItems(retrievedlength)
                # log the time of last succesful post
                self._interval_timestamp = time.time()

    def _process_post(self, data):
        """
        To be implemented in subclass.

        :return: True if data posted successfully and can be discarded
        """
        pass

    def _send_post(self, post_url, post_body=None):
        """

        :param post_url:
        :param post_body:
        :return: the received reply if request is successful
        """
        """Send data to server.

        data (list): node and values (eg: '[node,val1,val2,...]')
        time (int): timestamp, time when sample was recorded

        return True if data sent correctly

        """

        reply = ""
        request = urllib2.Request(post_url, post_body)
        try:
            response = urllib2.urlopen(request, timeout=60)
        except urllib2.HTTPError as e:
            self._log.warning("Couldn't send to server, HTTPError: " +
                              str(e.code))
        except urllib2.URLError as e:
            self._log.warning("Couldn't send to server, URLError: " +
                              str(e.reason))
        except httplib.HTTPException:
            self._log.warning("Couldn't send to server, HTTPException")
        except Exception:
            import traceback
            self._log.warning("Couldn't send to server, Exception: " +
                              traceback.format_exc())
        else:
            reply = response.read()
        finally:
            return reply

"""class EmonHubEmoncmsDispatcher

Stores server parameters and buffers the data between two HTTP requests

"""


class EmonHubEmoncmsDispatcher(EmonHubDispatcher):

    def __init__(self, dispatcherName, queue, **kwargs):
        """Initialize dispatcher

        """

        # Initialization
        super(EmonHubEmoncmsDispatcher, self).__init__(dispatcherName, queue, **kwargs)

        # add or alter any default settings for this dispatcher
        self._defaults.update({'maxItemsPerPost': 100, 'url': 'http://emoncms.org'})
        self._defaults.update({'emoncmsinterval': 3600, 'emoncms': False})
        self._settings.update({'apikey': ''})

        # This line will stop the default values printing to logfile at start-up
        self._settings.update(self._defaults)

        # set an absolute upper limit for number of items to process per post
        self._item_limit = 250

        # Initialize additional interval timer for emoncms last ping timestamp
        self._emoncmsinterval_timestamp = 0

    def action(self):
        """

        :return:
        """
        # Perform the standard action tasks
        super(EmonHubEmoncmsDispatcher, self).action()

        # If an pinginterval is set, check if that time has passed since last post
        if int(self._settings['emoncmsinterval']) and time.time() - self._emoncmsinterval_timestamp < int(self._settings['emoncmsinterval']):
            return
        else:
            # Then ping emoncms server
            if self._ping_emoncms():
                self._emoncmsinterval_timestamp = time.time()

    def _process_post(self, databuffer):
        """Send data to server."""
        
        # databuffer is of format:
        # [[timestamp, nodeid, datavalues][timestamp, nodeid, datavalues]]
        # [[1399980731, 10, 150, 250 ...]]

        if not 'apikey' in self._settings.keys() or str.lower(self._settings['apikey'][:4]) == 'xxxx':
            return

        data_string = json.dumps(databuffer, separators=(',', ':'))
        
        # Prepare URL string of the form
        # http://domain.tld/emoncms/input/bulk.json?apikey=12345
        # &data=[[0,10,82,23],[5,10,82,23],[10,10,82,23]]
        # &sentat=15' (requires emoncms >= 8.0)

        # time that the request was sent at
        sentat = int(time.time())

        # Construct post_url (without apikey)
        post_url = self._settings['url']+'/input/bulk'+'.json?apikey='
        post_body = "data="+data_string+"&sentat="+str(sentat)

        # logged before apikey added for security
        self._log.info("Sending: " + post_url + "E-M-O-N-C-M-S-A-P-I-K-E-Y&" + post_body)

        # Add apikey to post_url
        post_url = post_url + self._settings['apikey']

        # The Develop branch of emoncms allows for the sending of the apikey in the post
        # body, this should be moved from the url to the body as soon as this is widely
        # adopted

        reply = self._send_post(post_url, post_body)
        if reply == 'ok':
            self._log.debug("Receipt acknowledged with '" + reply + "' from " + self._settings['url'])
            return True
        else:
            self._log.warning("Send failure: wanted 'ok' but got "+reply)

    def _ping_emoncms(self):
        """

        :return:
        """

        if not self._settings['emoncms'] in ('myip','hub'):
            return
        self._log.info("Updating IP address for emonHub at: " + self._settings['url'])
        post = str(self._settings['url']+ "/" + self._settings['emoncms'] \
                   + "/set.json?apikey=" + self._settings['apikey'])

        if self._settings['emoncms'] == 'hub':
            post = post + "&hubid=Hub1" + "&hubtime=" + str(int(time.time()))
            # TODO temporarily added timestamp & hard-coded hubid

        reply = self._send_post(post)
        msg = '"IP address set to: '
        if msg in str(reply):
            self._log.debug(self._settings['url'] + " confirmed " + reply)
            return True
        else:
            self._log.warning('IP address update failure: wanted ' + msg +'" but got '+reply)
            return False

"""class EmonHubDispatcherInitError

Raise this when init fails.

"""


class EmonHubDispatcherInitError(Exception):
    pass
