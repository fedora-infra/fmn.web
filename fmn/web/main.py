# -*- coding: utf-8 -*-
""" The flask application """

# These two lines are needed to run on EL6
__requires__ = ['SQLAlchemy >= 0.7', 'jinja2 >= 2.4']  # NOQA
import pkg_resources  # NOQA

from fmn.web.app import app


if __name__ == '__main__':
    app.debug = True
    print('Running the FMN web application from fmn/web/main.py is deprecated,'
          ' please use runserver.py in the root of the repository.')
    app.run()
