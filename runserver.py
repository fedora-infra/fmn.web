#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run the flask application in a development server"""

# These two lines are needed to run on EL6
__requires__ = ['SQLAlchemy >= 0.7', 'jinja2 >= 2.4']  # NOQA
import pkg_resources  # NOQA

import argparse
import os

from fmn.web.app import app


parser = argparse.ArgumentParser(
    description='Run the anitya app')
parser.add_argument(
    '--config', '-c', dest='config',
    help='Configuration file to use for fmn.web')
parser.add_argument(
    '--debug', dest='debug', action='store_true',
    default=False,
    help='Expand the level of data returned.')
parser.add_argument(
    '--profile', dest='profile', action='store_true',
    default=False,
    help='Profile the anitya application.')
parser.add_argument(
    '--port', '-p', default=5000,
    help='Port for the flask application.')
parser.add_argument(
    '--host', default='127.0.0.1',
    help='IP address for the flask application to bind to.'
)

args = parser.parse_args()


if args.profile:
    from werkzeug.contrib.profiler import ProfilerMiddleware
    app.config['PROFILE'] = True
    app.wsgi_app = ProfilerMiddleware(app.wsgi_app, restrictions=[30])


if args.config:
    config = args.config
    if not config.startswith('/'):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)))
        config = os.path.join(here, config)
    os.environ['FMN_WEB_CONFIG'] = config


app.debug = True
app.run(port=int(args.port), host=args.host)
