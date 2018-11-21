# pylint: skip-file
"""
SkydioClient
"""

# Prep for python3
from __future__ import absolute_import
from __future__ import print_function

import argparse
import base64
import json
import os
import sys
import time
import urllib2
from uuid import uuid4


def fmt_out(fmt, *args, **kwargs):
    """ Helper for printing formatted text to stdout. """
    sys.stdout.write(fmt.format(*args, **kwargs))
    sys.stdout.flush()


def fmt_err(fmt, *args, **kwargs):
    """ Helper for printing formatted text to stderr. """
    sys.stderr.write(fmt.format(*args, **kwargs))
    sys.stderr.flush()


def save_image(client, filename='image.png'):
    """
    Fetch raw image data from the vehicle and and save it as png, using opencv
    """
    import cv2
    import numpy

    t1 = time.time()
    # Fetch the image metadata for the latest color image.
    data = client.request_json('channel/SUBJECT_CAMERA_RIG_NATIVE')
    t2 = time.time()
    fmt_out('Got metadata in {}ms\n', int(1000 * (t2 - t1)))
    images = data['json']['images']
    if not images:
        return

    # Download the raw pixel data from the vehicle's shared memory.
    # Note that this is not a high-speed image api, as it uses uncompressed image data over HTTP.
    image = images[0]
    image_path = image['data']
    url = '{}/shm{}'.format(client.baseurl, image_path)
    try:
        request = urllib2.Request(url)
        response = urllib2.urlopen(request)
        image_data = response.read()
    except urllib2.HTTPError as err:
        fmt_err('Got error for url {} {}\n', image_path, err)
        return
    t3 = time.time()
    fmt_out('Got image data in {}ms\n', int(1000 * (t3 - t2)))

    # Convert and save as a PNG
    pixfmt = image['pixelformat']
    PIXELFORMAT_YUV = 1009
    PIXELFORMAT_RGB = 1002
    if pixfmt == PIXELFORMAT_YUV:
        bytes_per_pixel = 2
        conversion_format = cv2.COLOR_YUV2BGR_UYVY
    elif pixfmt == PIXELFORMAT_RGB:
        bytes_per_pixel = 3
        conversion_format = cv2.COLOR_RGB2BGR
    else:
        fmt_err('Unsupported pixelformat {}\n', pixfmt)
        return
    width = image['width']
    height = image['height']
    num_bytes = width * height * bytes_per_pixel
    input_array = numpy.array([numpy.uint8(ord(c)) for c in image_data[:num_bytes]])
    input_array.shape = (height, width, bytes_per_pixel)
    bgr_array = cv2.cvtColor(input_array, conversion_format)
    cv2.imwrite(filename, bgr_array)
    t4 = time.time()
    fmt_out('Saved image in {}ms\n', int(1000 * (t4 - t3)))

    return filename


class SkydioClient(object):
    """
    HTTP client for communicating with a Skill running on a Skydio drone.

    Use this to connect a laptop over Wifi or an onboard computer over ethernet.

    Args:
        baseurl (str): The url of the vehicle.
            If you're directly connecting to a real R1 via WiFi, use 192.168.10.1
            If you're connected to a simulator over the Internet, use https://sim####.sim.skydio.com

        pilot (bool): Set to True in order to directly control the drone. Disables phone access.

        token_file (str): Path to a file that contains the auth token for simulator access.
    """

    def __init__(self, baseurl, pilot=False, token_file=None):
        self.baseurl = baseurl
        self.access_token = None
        self.session_id = None
        self.access_level = None
        self._authenticate(pilot, token_file)

    def _authenticate(self, pilot=False, token_file=None):
        """ Request an access token from the vehicle. If using a sim, a token_file is required. """
        request = {
            'client_id': str(uuid4()),
            'requested_level': (8 if pilot else 4),
            'commandeer': True,
        }

        if token_file:
            if not os.path.exists(token_file):
                fmt_err("Token file does not exist: {}\n", token_file)
                sys.exit(1)

            with open(token_file, 'r') as tokenf:
                token = tokenf.read()
                request['credentials'] = token.strip()

        response = self.request_json('authentication', request)
        self.access_level = response.get('accessLevel')
        if pilot and self.access_level != 'PILOT':
            fmt_err("Did not successfully auth as pilot\n")
            sys.exit(1)
        self.access_token = response.get('accessToken')

    def request_json(self, endpoint, json_data=None, timeout=20):
        """ Send a GET or POST request to the vehicle and get a parsed JSON response.

        Args:
            endpoint (str): the path to request.
            json_data (dict): an optional JSON dictionary to send.
            timeout (int): number of seconds to wait for a response.

        Raises:
            HTTPError: if the server responds with 4XX or 5XX status code
            IOError: if the response body cannot be read.
            RuntimeError: if the response is poorly formatted.

        Returns:
            dict: the servers JSON response
        """
        url = '{}/api/{}'.format(self.baseurl, endpoint)
        headers = {'Accept': 'application/json'}
        if self.access_token:
            headers['Authorization'] = 'Bearer {}'.format(self.access_token)
        if json_data is not None:
            headers['Content-Type'] = 'application/json'
            request = urllib2.Request(url, json.dumps(json_data), headers=headers)
        else:
            request = urllib2.Request(url, headers=headers)
        response = urllib2.urlopen(request, timeout=timeout)
        status_code = response.getcode()
        status_code_class = int(status_code / 100)
        if status_code_class in [4, 5]:
            raise urllib2.HTTPError(url, status_code, '{} Client Error'.format(status_code),
                                    response.info(), response)
        # Ensure that the request is a file like object with a read() method.
        # We've seen instances where urlopen does not raise an exception, but we cannot read it.
        if not callable(getattr(response, 'read', None)):
            raise IOError('urlopen response has no read() method')

        server_response = json.loads(response.read())
        if 'data' not in server_response:
            # The server detected an error. Display it.
            raise RuntimeError('No response data: {}'.format(server_response.get('error')))
        return server_response['data']

    def send_custom_comms(self, skill_key, data, no_response=False):
        """
        Send custom bytes to the vehicle and optionally return a response

        Args:
            skill_key (str): The identifer for the Skill you want to receive this message.
            data (bytes): The payload to send.
            no_response (bool): Set this to True if you don't want a response.

        Returns:
            dict: a dict with metadata for the response and a 'data' field, encoded by the Skill.
        """

        rpc_request = {
            'data': base64.b64encode(data),
            'skill_key': skill_key,
            'no_response': no_response,  # this key is option and defaults to False
        }

        # Post rpc to the server as json.
        try:
            rpc_response = self.request_json('custom_comms', rpc_request)
        except Exception as error:  # pylint: disable=broad-except
            fmt_err('Comms Error: {}\n', error)
            return None

        # Parse and return the rpc.
        if rpc_response:
            if 'data' in rpc_response:
                rpc_response['data'] = base64.b64decode(rpc_response['data'])
        return rpc_response

    def update_pilot_status(self):
        """ Ping the vehicle to keep session alive, and get status back. """
        args = {
            'wouldAcceptPilot': True,
            'inForeground': True,
            'mediaMode': 'FLIGHT_CONTROL',
            'takeoffType': 'GROUND_TAKEOFF',
        }
        if self.session_id:
            args['sessionId'] = self.session_id
        response = self.request_json('status', args)
        self.session_id = response['sessionId']
        return response

    def takeoff(self):
        """ Request takeoff. Blocks until flying. """
        if self.access_level != 'PILOT':
            fmt_err('Cannot takeoff: not pilot\n')
            return

        self.update_pilot_status()
        self.disable_faults()

        while 1:
            time.sleep(1)  # downsample to prevent spamming the endpoint
            phase = self.update_pilot_status().get('flightPhase')
            if not phase:
                continue
            fmt_out('flight phase = {}\n', phase)
            if phase == 'READY_FOR_GROUND_TAKEOFF':
                fmt_out('Publishing ground takeoff\n')
                self.request_json('async_command', {'command': 'ground_takeoff'})
            elif phase == 'FLYING':
                fmt_out('Flying.\n')
                return

    def land(self):
        """ Land the vehicle. Blocks until on the ground. """
        if self.access_level != 'PILOT':
            fmt_err('Cannot land: not pilot\n')
            return

        phase = 'FLYING'
        while phase == 'FLYING':
            fmt_out('Sending LAND\n')
            self.request_json('async_command', {'command': 'land'})
            time.sleep(1)
            new_phase = self.update_pilot_status().get('flightPhase')
            if not new_phase:
                continue
            phase = new_phase

    def set_skill(self, skill_key):
        """ Request a specific skill to be active. """
        if self.access_level != 'PILOT':
            fmt_err('Cannot switch skills: not pilot\n')
            return
        fmt_out("Requesting {} skill\n", skill_key)
        endpoint = 'set_skill/{}'.format(skill_key)
        self.request_json(endpoint, {'args': {}})

    def disable_faults(self):
        """ Tell the vehicle to ignore missing phone info. """
        faults = {
            # These faults occur if phone isn't connected via UDP
            'LOST_PHONE_COMMS_SHORT': 2,
            'LOST_PHONE_COMMS_LONG': 3,
        }
        for _, fault_id in faults.items():
            self.request_json('set_fault_override/{}'.format(fault_id),
                              {'override_on': True, 'fault_active': False})


def main():
    parser = argparse.ArgumentParser(description="Example command-line interface for a Skill.")
    parser.add_argument('--baseurl', metavar='URL', default='http://192.168.10.1',
                        help='the url of the vehicle')

    # NOTE: you'll need to change this skill key to match your copy of com_link
    parser.add_argument('--skill-key', metavar='KEY', default='samples.com_link.ComLink',
                        help='the skill to communicate with')

    # NOTE: you'll need a token file in order to connect to a simulator.
    # Tokens are NOT required for real R1s.
    parser.add_argument('--token-file',
                        help='path to the auth token for your simulator')

    # Pilot operations (if you dont want to use a phone)
    parser.add_argument('--pilot', action='store_true',
                        help='become the pilot device (instead of using a phone)')
    parser.add_argument('--takeoff', action='store_true',
                        help='send a takeoff command (must be pilot)')
    parser.add_argument('--land', action='store_true',
                        help='send a land command (must be pilot)')

    # Example actions for the ComLink skill
    parser.add_argument('--title', default='Hello World',
                        help='set the title on the phone')
    parser.add_argument('--forward', metavar='X', type=float,
                        help='move forward X meters.')
    parser.add_argument('--loop', action='store_true',
                        help='keep sending messages')

    # Experimental: save an image from the vehicle as a .png file
    parser.add_argument('--image', action='store_true',
                        help='save an image')

    args = parser.parse_args()

    # Create the client to use for all requests.
    client = SkydioClient(args.baseurl, args.pilot, args.token_file)

    if args.takeoff:
        client.takeoff()

    if args.pilot:
        # Ensure we switch to the specified skill.
        # NOTE: You must have already sent this skill to the vehicle via a phone.
        client.set_skill(args.skill_key)

    # Example usage: repeatedly send some data and print the response.
    start_time = time.time()
    while 1:
        elapsed_time = int(time.time() - start_time)
        request = {
            'title': args.title,
            'detail': elapsed_time,
        }
        if args.forward:
            request['forward'] = args.forward

        fmt_out('Custom Comms Request {}\n', request)

        # Arbitrary data format. Using JSON here.
        data = json.dumps(request)

        response = client.send_custom_comms(args.skill_key, data)
        fmt_out('Custom Comms Response {}\n', json.dumps(response, sort_keys=True, indent=True))

        if args.image:
            fmt_out('Requesting image\n')
            save_image(client, filename='image_{}.png'.format(elapsed_time))

        if args.loop:
            # Rate-limit to prevent overloading the vehicle.
            time.sleep(1)
        else:
            break

    if args.land:
        client.land()


if __name__ == '__main__':
    main()
