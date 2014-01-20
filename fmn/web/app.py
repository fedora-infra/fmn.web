import codecs
import functools
import os
from bunch import Bunch
from pkg_resources import get_distribution

import docutils
import docutils.examples
import fedora.client
import fedmsg.config
import jinja2
import libravatar
import markupsafe

import flask
import sqlalchemy
from flask.ext.openid import OpenID

import fmn.lib
import fmn.lib.models
import fmn.web.converters
import fmn.web.forms

# Create the application.
app = flask.Flask(__name__)

app.url_map.converters['not_reserved'] = fmn.web.converters.NotReserved

# set up FAS
app.config.from_object('fmn.web.default_config')
if 'FMN_WEB_CONFIG' in os.environ:  # pragma: no cover
    app.config.from_envvar('FMN_WEB_CONFIG')

# Set up OpenID in stateless mode
oid = OpenID(app, store_factory=lambda: None)

# Inject a simple jinja2 test -- it is surprising jinja2 does not have this.
app.jinja_env.tests['equalto'] = lambda x, y: x == y

fedmsg_config = fedmsg.config.load_config()
db_url = fedmsg_config.get('fmn.sqlalchemy.uri')
if not db_url:
    raise ValueError("fmn.sqlalchemy.uri must be present")

valid_paths = fmn.lib.load_rules(root="fmn.rules")

SESSION = fmn.lib.models.init(db_url, debug=False, create=False)


def extract_openid_identifier(openid_url):
    openid = openid_url.split('://')[1]
    if openid.endswith('/'):
        openid = openid[:-1]
    if 'id?id=' in openid:
        openid = openid.split('id?id=')[1]
    if 'me.yahoo.com/a/' in openid:
        openid = openid.split('me.yahoo.com/a/')[1]
    openid = openid.replace('/', '_')
    return openid


@app.before_request
def check_auth():
    flask.g.auth = Bunch(
        logged_in=False,
        method=None,
        id=None,
    )
    if 'openid' in flask.session:
        openid = extract_openid_identifier(flask.session.get('openid'))
        flask.g.auth.logged_in = True
        flask.g.auth.method = u'openid'
        flask.g.auth.openid = openid
        flask.g.auth.openid_url = flask.session.get('openid')
        flask.g.auth.fullname = flask.session.get('fullname', None)
        flask.g.auth.nickname = flask.session.get('nickname', None)
        flask.g.auth.email = flask.session.get('email', None)


@oid.after_login
def after_openid_login(resp):
    default = flask.url_for('index')
    if resp.identity_url:
        openid_url = resp.identity_url
        flask.session['openid'] = openid_url
        flask.session['fullname'] = resp.fullname
        flask.session['nickname'] = resp.nickname or resp.fullname
        flask.session['email'] = resp.email
        next_url = flask.request.args.get('next', default)
        return flask.redirect(next_url)
    else:
        return flask.redirect(default)


@app.teardown_request
def shutdown_session(exception=None):
    """ Remove the DB session at the end of each request. """
    SESSION.remove()


def admin(user):
    return user in app.config.get('FMN_ADMINS', [])


class APIError(Exception):
    def __init__(self, status_code, errors):
        self.status_code = status_code
        self.errors = errors


def login_required(function):
    """ Flask decorator to retrict access to logged-in users. """
    @functools.wraps(function)
    def decorated_function(*args, **kwargs):
        """ Decorated function, actually does the work. """
        if not flask.g.auth.logged_in:
            flask.flash('Login required', 'errors')
            return flask.redirect(
                flask.url_for('login', next=flask.request.url))

        # Ensure that the logged in user exists before we proceed.
        user = fmn.lib.models.User.get_or_create(
            SESSION,
            openid=flask.g.auth.openid,
            openid_url=flask.g.auth.openid_url,
        )

        return function(*args, **kwargs)
    return decorated_function


def api_method(function):
    """ A decorator to handle common API output stuff. """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            result = function(*args, **kwargs)
        except APIError as e:
            app.logger.exception(e)
            response = flask.jsonify(e.errors)
            response.status_code = e.status_code
        else:
            # Redirect browsers to the object.
            # otherwise, return json response to api clients.
            if 'url' in result and request_wants_html():
                response = flask.redirect(result['url'])
            else:
                response = flask.jsonify(result)
                response.status_code = 200
        return response

    return wrapper


def request_wants_html():
    """ accept header returns json type content only
    http://flask.pocoo.org/snippets/45/
    """
    best = flask.request.accept_mimetypes \
        .best_match(['application/json', 'text/html', 'text/plain'])
    return best == 'text/html' and \
        flask.request.accept_mimetypes[best] > (
            flask.request.accept_mimetypes['application/json'] or
            flask.request.accept_mimetypes['text/plain'])


@app.context_processor
def inject_variable():
    """ Inject into all templates variables that we would like to have all
    the time.
    """
    openid = None
    contexts = []
    if flask.g.auth.logged_in:
        openid = flask.g.auth.openid
        contexts = fmn.lib.models.Context.all(SESSION)

    web_version = get_distribution('fmn.web').version
    lib_version = get_distribution('fmn.lib').version
    rules_version = get_distribution('fmn.rules').version

    return dict(openid=openid,
                contexts=contexts,
                valid_paths=valid_paths,
                web_version=web_version,
                lib_version=lib_version,
                rules_version=rules_version)


@app.route('/_heartbeat')
def heartbeat():
    """ An endpoint so haproxy can know we're alive. """
    return "Lub-Dub"


@app.route('/')
def index():
    return flask.render_template(
        'index.html',
        current='index',
        contexts=fmn.lib.models.Context.all(SESSION),
    )


@app.route('/about')
def about():
    return flask.render_template(
        'docs.html',
        current='about',
        docs=load_docs(flask.request),
    )


@app.route('/<not_reserved:openid>')
@app.route('/<not_reserved:openid>/')
@login_required
def profile(openid):

    if (not flask.g.auth.logged_in or (
        flask.g.auth.openid != openid and
            not admin(flask.g.auth.openid))):

        flask.abort(403)

    user = fmn.lib.models.User.get_or_create(
        SESSION,
        openid=flask.g.auth.openid,
        openid_url=flask.g.auth.openid_url,
    )
    avatar = libravatar.libravatar_url(
        openid=user.openid_url,
        https=app.config.get('FMN_SSL', False),
        size=140)

    prefs = fmn.lib.models.Preference.by_user(
        SESSION, openid, allow_none=False)

    icons = {}
    for context in fmn.lib.models.Context.all(SESSION):
        icons[context.name] = context.icon

    return flask.render_template(
        'profile.html',
        current='profile',
        avatar=avatar,
        prefs=prefs,
        icons=icons,
        api_key=user.api_key,
        fedora_mobile=flask.request.args.get('fedora_mobile') == 'true',
        openid_url=flask.g.auth.openid)

@app.route('/reset-api-key')
@app.route('/reset-api-key/')
@login_required
def reset_api_key():
    if not flask.g.auth.logged_in:
        flask.abort(403)

    user = fmn.lib.models.User.get_or_create(
        SESSION,
        openid=flask.g.auth.openid,
        openid_url=flask.g.auth.openid_url,
    )

    user.reset_api_key(SESSION)
    return flask.redirect(flask.url_for('profile', openid=flask.g.auth.openid))


@app.route('/<not_reserved:openid>/<context>')
@app.route('/<not_reserved:openid>/<context>/')
@login_required
def context(openid, context):
    if flask.g.auth.openid != openid and not admin(flask.g.auth.openid):
        flask.abort(403)

    context = fmn.lib.models.Context.by_name(SESSION, context)
    if not context:
        flask.abort(404)

    pref = fmn.lib.models.Preference.get_or_create(
        SESSION, openid=openid, context=context)

    return flask.render_template(
        'context.html',
        current=context.name,
        context=context,
        confirmation=context.get_confirmation(openid),
        preference=pref)


@app.route('/<not_reserved:openid>/<context>/<filter_name>')
@app.route('/<not_reserved:openid>/<context>/<filter_name>/')
@login_required
def filter(openid, context, filter_name):
    if flask.g.auth.openid != openid and not admin(flask.g.auth.openid):
        flask.abort(403)

    context = fmn.lib.models.Context.by_name(SESSION, context)
    if not context:
        flask.abort(404)

    pref = fmn.lib.models.Preference.get_or_create(
        SESSION, openid=openid, context=context)

    filter = None
    for _filter in pref.filters:
        if _filter.name == filter_name:
            filter = _filter
            break

    if not pref.has_filter(SESSION, filter_name):
        flask.abort(404)

    filter = pref.get_filter(SESSION, filter_name)

    return flask.render_template(
        'filter.html',
        current=context.name,
        filter=filter)


@app.route('/confirm/<action>/<secret>')
@app.route('/confirm/<action>/<secret>/')
@login_required
def handle_confirmation(action, secret):

    if action not in ['accept', 'reject']:
        flask.abort(404)

    confirmation = fmn.lib.models.Confirmation.by_secret(SESSION, secret)

    if not confirmation:
        flask.abort(404)

    if flask.g.auth.openid != confirmation.openid:
        flask.abort(403)

    if action == 'accept':
        confirmation.set_status(SESSION, 'accepted')
    else:
        confirmation.set_status(SESSION, 'rejected')

    return flask.redirect(flask.url_for(
        'context',
        openid=confirmation.openid,
        context=confirmation.context_name))


@app.route('/api/filter', methods=['POST', 'DELETE'])
@api_method
def handle_filter():
    form = fmn.web.forms.FilterForm(flask.request.form)

    if not form.validate():
        raise APIError(400, form.errors)

    openid = form.openid.data
    context = form.context.data
    filter_name = form.filter_name.data
    method = (form.method.data or flask.request.method).upper()

    if flask.g.auth.openid != openid and not admin(flask.g.auth.openid):
        raise APIError(403, dict(reason="%r is not %r" % (
            flask.g.auth.openid, openid
        )))

    if method not in ['POST', 'DELETE']:
        raise APIError(405, dict(reason="Only POST and DELETE accepted"))

    user = fmn.lib.models.User.by_openid(SESSION, openid)
    if not user:
        raise APIError(403, dict(reason="%r is not a user" % openid))

    ctx = fmn.lib.models.Context.by_name(SESSION, context)
    if not ctx:
        raise APIError(403, dict(reason="%r is not a context" % context))

    pref = fmn.lib.models.Preference.get_or_create(
        SESSION, openid=openid, context=ctx)

    try:
        if method == 'POST':
            # Ensure that a filter with this name doesn't already exist.
            if pref.has_filter(SESSION, filter_name):
                raise APIError(404, dict(
                    reason="%r already exists" % filter_name))

            filter = fmn.lib.models.Filter.create(SESSION, filter_name)
            pref.add_filter(SESSION, filter)
            next_url = flask.url_for(
                'filter',
                openid=openid,
                context=context,
                filter_name=filter_name,
            )
        elif method == 'DELETE':
            filter = pref.get_filter(SESSION, filter_name)
            SESSION.delete(filter)
            SESSION.commit()
            next_url = flask.url_for(
                'context',
                openid=openid,
                context=context,
            )
        else:
            raise NotImplementedError("This is impossible.")
    except (ValueError, KeyError) as e:
        app.logger.exception(e)
        raise APIError(403, dict(reason=str(e)))

    return dict(message="ok", url=next_url)


def int_or_none(value):
    """ Cast form fields to integers ourselves.

    form.validate() could potentially do this for us, but I don't know how to
    make an IntegerField also accept None.
    """
    if value == "<disabled>":
        return None

    try:
        return int(value)
    except TypeError:
        raise APIError(400, dict(batch_delta=["Not a valid integer value"]))


@app.route('/api/details', methods=['POST'])
@api_method
def handle_details():
    form = fmn.web.forms.DetailsForm(flask.request.form)

    if not form.validate():
        raise APIError(400, form.errors)

    openid = form.openid.data
    context = form.context.data
    detail_value = form.detail_value.data
    batch_delta = form.batch_delta.data
    batch_count = form.batch_count.data
    toggle_enable = form.toggle_enable.data

    if flask.g.auth.openid != openid and not admin(flask.g.auth.openid):
        raise APIError(403, dict(reason="%r is not %r" % (
            flask.g.auth.openid, openid
        )))

    user = fmn.lib.models.User.by_openid(SESSION, openid)
    if not user:
        raise APIError(403, dict(reason="%r is not a user" % openid))

    ctx = fmn.lib.models.Context.by_name(SESSION, context)
    if not ctx:
        raise APIError(403, dict(reason="%r is not a context" % context))

    pref = fmn.lib.models.Preference.get_or_create(
        SESSION, openid=openid, context=ctx)

    # Are they changing a delivery detail?
    if detail_value and detail_value != pref.detail_value:
        # Do some validation on the specifics of the value before we commit.
        try:
            fmn.lib.validate_detail_value(ctx, detail_value)
        except Exception as e:
            raise APIError(403, dict(reason=str(e)))

        # We need to *VERIFY* that they really have this delivery detail
        # before we start doing stuff.  Otherwise, ralph could put in pingou's
        # email address and spam the crap out of him.
        if fedmsg_config.get('fmn.verify_delivery_details', True):
            con = fmn.lib.models.Confirmation.get_or_create(
                SESSION, openid=openid, context=ctx)
            con.set_value(SESSION, detail_value)
            con.set_status(SESSION, 'pending')
        else:
            # Otherwise, just change the details right away.  Never do this.
            pref.update_details(SESSION, detail_value)

    # Let them change batch_delta and batch_count as they please.
    if batch_delta or batch_count:
        batch_delta = int_or_none(batch_delta)
        batch_count = int_or_none(batch_count)
        pref.set_batch_values(SESSION, delta=batch_delta, count=batch_count)

    # Also, let them enable or disable as they please.
    if toggle_enable:
        pref.set_enabled(SESSION, not pref.enabled)

    next_url = flask.url_for(
        'context',
        openid=openid,
        context=context,
    )

    return dict(message="ok", url=next_url)


@app.route('/api/rule', methods=['POST'])
@api_method
def handle_rule():
    form = fmn.web.forms.RuleForm(flask.request.form)

    if not form.validate():
        raise APIError(400, form.errors)

    openid = form.openid.data
    context = form.context.data
    filter_name = form.filter_name.data
    code_path = form.rule_name.data
    method = (form.method.data or flask.request.method).upper()
    # Extract arguments to rules using the extra information provided
    known_args = ['openid', 'filter_name', 'context', 'rule_name']
    arguments = {}
    for args in flask.request.form:
        if args not in known_args:
            arguments[args] = flask.request.form[args]

    if flask.g.auth.openid != openid and not admin(flask.g.auth.openid):
        raise APIError(403, dict(reason="%r is not %r" % (
            flask.g.auth.openid, openid
        )))

    if method not in ['POST', 'DELETE']:
        raise APIError(405, dict(reason="Only POST and DELETE accepted"))

    user = fmn.lib.models.User.by_openid(SESSION, openid)
    if not user:
        raise APIError(403, dict(reason="%r is not a user" % openid))

    ctx = fmn.lib.models.Context.by_name(SESSION, context)
    if not ctx:
        raise APIError(403, dict(reason="%r is not a context" % context))

    pref = fmn.lib.models.Preference.get_or_create(
        SESSION, openid=openid, context=ctx)

    if not pref.has_filter(SESSION, filter_name):
        raise APIError(403, dict(reason="%r is not a filter" % filter_name))

    filter = pref.get_filter(SESSION, filter_name)

    try:
        if method == 'POST':
            filter.add_rule(SESSION, valid_paths, code_path, **arguments)
        elif method == 'DELETE':
            filter.remove_filter(SESSION, code_path)  # , **arguments)
        else:
            raise NotImplementedError("This is impossible.")
    except (ValueError, KeyError) as e:
        app.logger.exception(e)
        raise APIError(403, dict(reason=str(e)))

    next_url = flask.url_for(
        'filter',
        openid=openid,
        context=context,
        filter_name=filter_name,
    )

    return dict(message="ok", url=next_url)


@app.route('/login/', methods=('GET', 'POST'))
@app.route('/login', methods=('GET', 'POST'))
@oid.loginhandler
def login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    if flask.g.auth.logged_in:
        return flask.redirect(next_url)

    openid_server = flask.request.form.get('openid', None)
    if openid_server:
        return oid.try_login(
            openid_server, ask_for=['email', 'fullname', 'nickname'])

    return flask.render_template(
        'login.html', next=oid.get_next_url(), error=oid.fetch_error())


@app.route('/login/fedora/')
@app.route('/login/fedora')
@oid.loginhandler
def fedora_login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    return oid.try_login(
        app.config['FMN_FEDORA_OPENID'],
        ask_for=['email', 'fullname', 'nickname'])

@app.route('/login/google/')
@app.route('/login/google')
@oid.loginhandler
def google_login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    return oid.try_login(
        "https://www.google.com/accounts/o8/id",
        ask_for=['email', 'fullname'])

@app.route('/login/yahoo/')
@app.route('/login/yahoo')
@oid.loginhandler
def yahoo_login():
    default = flask.url_for('index')
    next_url = flask.request.args.get('next', default)
    return oid.try_login(
        "https://me.yahoo.com/",
        ask_for=['email', 'fullname'])


@app.route('/logout/')
@app.route('/logout')
def logout():
    if 'openid' in flask.session:
        flask.session.pop('openid')
    return flask.redirect(flask.url_for('index'))


def modify_rst(rst):
    """ Downgrade some of our rst directives if docutils is too old. """

    try:
        # The rst features we need were introduced in this version
        minimum = [0, 9]
        version = map(int, docutils.__version__.split('.'))

        # If we're at or later than that version, no need to downgrade
        if version >= minimum:
            return rst
    except Exception:
        # If there was some error parsing or comparing versions, run the
        # substitutions just to be safe.
        pass

    # Otherwise, make code-blocks into just literal blocks.
    substitutions = {
        '.. code-block:: javascript': '::',
    }
    for old, new in substitutions.items():
        rst = rst.replace(old, new)

    return rst


def modify_html(html):
    """ Perform style substitutions where docutils doesn't do what we want.
    """

    substitutions = {
        '<tt class="docutils literal">': '<code>',
        '</tt>': '</code>',
    }
    for old, new in substitutions.items():
        html = html.replace(old, new)

    return html


def preload_docs(endpoint):
    """ Utility to load an RST file and turn it into fancy HTML. """

    here = os.path.dirname(os.path.abspath(__file__))
    fname = os.path.join(here, 'docs', endpoint + '.rst')
    with codecs.open(fname, 'r', 'utf-8') as f:
        rst = f.read()

    rst = modify_rst(rst)
    api_docs = docutils.examples.html_body(rst)
    api_docs = modify_html(api_docs)
    api_docs = markupsafe.Markup(api_docs)
    return api_docs

htmldocs = dict.fromkeys(['about'])
for key in htmldocs:
    htmldocs[key] = preload_docs(key)


def load_docs(request):
    URL = fedmsg_config.get('fmn.base_url', request.url_root)
    docs = htmldocs[request.endpoint]
    docs = jinja2.Template(docs).render(URL=URL)
    return markupsafe.Markup(docs)
