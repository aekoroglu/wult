# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Adam Hawley <adam.james.hawley@intel.com>

"""
Serve a local directory on localhost.

Wult HTML reports can't be viewed locally due to restrictions imposed by web-browsers which prevent
them from reading report files. To solve this issue, the files must be hosted as a web-server.
Hence, this script was created so that it can be used to view wult HTML reports. Intended usage:
 1. Run this script.
 2. Select the report directory or a directory containing multiple reports when prompted to select
    one. Following this, the script opens the default browser at 'localhost:8000'.
"""

# pylint: disable=wrong-import-position
import sys

if sys.version_info < (3,5):
    raise Exception("This script requires Python 3.5 or higher.")

import argparse
import http.server
import os
import webbrowser
from tkinter.filedialog import askdirectory

DIRECTORY = askdirectory()

def parseargs():
    """Configure an argument parser and parse user arguments."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", nargs="?", default="localhost",
        help="Host to serve the report on. Defaults to 'localhost'.")
    parser.add_argument("--port", nargs="?", default=8000, type=int,
        help="Port to serve the report on. Defaults to '8080'.")
    parser.add_argument("--headless", action="store_true",
        help="Run the script without trying to open the report in the default web browser.")
    return parser.parse_args()

def _init_server(host, port, portcount=10):
    """
    Tries to initialise an 'http.server.HTTPServer' on 'host':'port'. If unsuccessful on 'port',
    cycles through 'portcount' other ports until it succeeds or raises an error.

    Returns a tuple in the format '(httpd, port)' where 'httpd' is a 'http.server.HTTPServer'
    instance and 'port' is the integer representation of the successful port.
    """

    serverinit = False
    initport = port

    while not serverinit:
        try:
            server_address = (host, port)
            httpd = http.server.HTTPServer(server_address, http.server.SimpleHTTPRequestHandler)
            serverinit = True
        except OSError as err:
            print("Failed to create HTTP server at port '{port}': {err}".format(port=port, err=err))
            port += 1

            if port - initport >= portcount:
                raise Exception("unable to create HTTP server, tried ports in range "
                                "{iport}-{port}.".format(iport=initport, port=port-1)) from None

            print("Trying again with port '{port}'.".format(port=port))

    return httpd, port

def servedir(host="localhost", port=8000, headless=False):
    """
    Serve 'DIRECTORY' on 'host:port'. If not 'headless', opens the default browser to view the
    report.
    """

    # Providing a directory to 'SimpleHTTPRequestHandler' was not supported until Python 3.7.
    # To make this script compatible with Python 3.5+, use 'os.chdir()' as a workaround.
    os.chdir(DIRECTORY)

    httpd, port = _init_server(host, port)
    URL = "http://{host}:{port}/".format(host=host, port=port)

    print("Serving directory '{dir}' at '{url}'.".format(dir=DIRECTORY, url=URL))
    print("Opening in browser. Please close this window when you have finished viewing reports.\n")
    print("Web server logs:")

    if not headless:
        # Open the user's web browser with the directory open.
        webbrowser.open(URL)
    httpd.serve_forever()


if __name__ == "__main__":
    args = parseargs()
    servedir(args.host, args.port, args.headless)
