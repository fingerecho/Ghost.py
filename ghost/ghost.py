# -*- coding: utf-8 -*-
import os
import time
import codecs
import json
import logging
from functools import wraps
from PyQt4 import QtWebKit
from PyQt4.QtNetwork import QNetworkRequest, QNetworkAccessManager
from PyQt4 import QtCore
from PyQt4.QtGui import QApplication
from PyQt4 import QtNetwork


default_user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/535.2 " +\
    "(KHTML, like Gecko) Chrome/15.0.874.121 Safari/535.2"


logger = logging.getLogger('ghost')


class Logger(logging.Logger):
    @staticmethod
    def log(message, sender="Ghost", level="info"):
        if not hasattr(logger, level):
            raise Exception('invalid log level')
        getattr(logger, level)("%s: %s", sender, message)


class GhostWebPage(QtWebKit.QWebPage):
    """Overrides QtWebKit.QWebPage in order to intercept some graphical
    behaviours like alert(), confirm().
    Also intercepts client side console.log().
    """
    def javaScriptConsoleMessage(self, message, *args, **kwargs):
        """Prints client console message in current output stream."""
        super(GhostWebPage, self).javaScriptConsoleMessage(message, *args,
        **kwargs)
        log_type = "error" if "Error" in message else "info"
        Logger.log(message, sender="Frame", level=log_type)

    def javaScriptAlert(self, frame, message):
        """Notifies ghost for alert, then pass."""
        Ghost.alert = message
        Logger.log("alert('%s')" % message, sender="Frame")

    def javaScriptConfirm(self, frame, message):
        """Checks if ghost is waiting for confirm, then returns the right
        value.
        """
        if Ghost.confirm_expected is None:
            raise Exception('You must specified a value to confirm "%s"' %
                message)
        confirmation = Ghost.confirm_expected
        Ghost.confirm_expected = None
        Logger.log("confirm('%s')" % message, sender="Frame")
        return confirmation

    def javaScriptPrompt(self, frame, message, defaultValue, result):
        """Checks if ghost is waiting for prompt, then enters the right
        value.
        """
        if Ghost.prompt_expected is None:
            raise Exception('You must specified a value for prompt "%s"' %
                message)
        result.append(Ghost.prompt_expected)
        Ghost.prompt_expected = None
        Logger.log("prompt('%s')" % message, sender="Frame")
        return True


def can_load_page(func):
    """Decorator that specifies if user can expect page loading from
    this action. If expect_loading is set to True, ghost will wait
    for page_loaded event.
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if 'expect_loading' in kwargs:
            expect_loading = True
            del kwargs['expect_loading']
        else:
            expect_loading = False
        if expect_loading:
            self.loaded = False
            func(self, *args, **kwargs)
            return self.wait_for_page_loaded()
        return func(self, *args, **kwargs)
    return wrapper


def client_utils_required(func):
    """Decorator that checks avabality of Ghost client side utils,
    injects require javascript file instead.
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.global_exists('GhostUtils'):
            self.evaluate_js_file(
                os.path.join(os.path.dirname(__file__), 'utils.js'))
        return func(self, *args, **kwargs)
    return wrapper


class HttpRessource(object):
    """Represents an HTTP ressource.
    """
    def __init__(self, reply):
        self.url = unicode(reply.url().toString())
        self.http_status = reply.attribute(
            QNetworkRequest.HttpStatusCodeAttribute).toInt()[0]
        self.headers = {}
        for header in reply.rawHeaderList():
            self.headers[unicode(header)] = unicode(reply.rawHeader(header))
        self._reply = reply


class Ghost(object):
    """Ghost manages a QWebPage.

    :param user_agent: The default User-Agent header.
    :param wait_timeout: Maximum step duration in second.
    :param display: A boolean that tells ghost to displays UI.
    :param log_level: The logging level.
    """
    alert = None
    confirm_expected = None
    prompt_expected = None

    def __init__(self, user_agent=default_user_agent, wait_timeout=4,
            display=False, log_level=logging.WARNING):
        self.http_ressources = []

        self.user_agent = user_agent
        self.wait_timeout = wait_timeout
        self.display = display

        self.loaded = False

        self.app = QApplication(['ghost'])

        self.page = GhostWebPage(self.app)
        self.set_viewport_size(400, 300)

        self.page.loadFinished.connect(self._page_loaded)
        self.page.loadStarted.connect(self._page_load_started)

        self.manager = self.page.networkAccessManager()
        self.manager.finished.connect(self._request_ended)

        self.cookie_jar = QtNetwork.QNetworkCookieJar()
        self.manager.setCookieJar(self.cookie_jar)

        self.main_frame = self.page.mainFrame()

        logger.setLevel(log_level)

        if self.display:
            self.webview = QtWebKit.QWebView()
            self.webview.setPage(self.page)
            self.webview.show()

    def __del__(self):
        self.app.quit()

    @client_utils_required
    @can_load_page
    def click(self, selector):
        """Click the targeted element.

        :param selector: A CSS3 selector to targeted element.
        """
        if not self.exists(selector):
            raise Exception("Can't find element to click")
        return self.evaluate('GhostUtils.click("%s");' % selector)

    def close_webview(self):
        self.webview.close()

    class confirm:
        """Statement that tells Ghost how to deal with javascript confirm().
        """
        def __init__(self, confirm=True):
            self.confirm = confirm

        def __enter__(self):
            Ghost.confirm_expected = self.confirm

        def __exit__(self, type, value, traceback):
            Ghost.confirm_expected = None

    @property
    def content(self):
        """Returns current frame HTML as a string."""
        return unicode(self.main_frame.toHtml())

    @property
    def cookies(self):
        """Returns all cookies."""
        return self.cookie_jar.allCookies()

    def delete_cookies(self):
        """Deletes all cookies."""
        self.cookie_jar.setAllCookies([])

    @can_load_page
    def evaluate(self, script):
        """Evaluates script in page frame.

        :param script: The script to evaluate.
        """
        return (self.main_frame.evaluateJavaScript("%s" % script),
            self._release_last_ressources())

    def evaluate_js_file(self, path, encoding='utf-8'):
        """Evaluates javascript file at given path in current frame.
        Raises native IOException in case of invalid file.

        :param path: The path of the file.
        :param encoding: The file's encoding.
        """
        self.evaluate(codecs.open(path, encoding=encoding).read())

    def exists(self, selector):
        """Checks if element exists for given selector.

        :param string: The element selector.
        """
        return not self.main_frame.findFirstElement(selector).isNull()

    @client_utils_required
    def fill(self, selector, values):
        """Fills a form with provided values.

        :param selector: A CSS selector to the target form to fill.
        :param values: A dict containing the values.
        """
        if not self.exists(selector):
            raise Exception("Can't find form")
        return self.evaluate('GhostUtils.fill("%s", %s);' % (
            selector, unicode(json.dumps(values))))

    @client_utils_required
    @can_load_page
    def fire_on(self, selector, method):
        """Call method on element matching given selector.

        :param selector: A CSS selector to the target element.
        :param method: The name of the method to fire.
        :param expect_loading: Specifies if a page loading is expected.
        """
        return self.evaluate('GhostUtils.fireOn("%s", "%s");' % (
            selector, method))

    def global_exists(self, global_name):
        """Checks if javascript global exists.

        :param global_name: The name of the global.
        """
        return self.evaluate('!(typeof %s === "undefined");' %
            global_name)[0].toBool()

    def open(self, address, method='get'):
        """Opens a web page.

        :param address: The ressource URL.
        :param method: The Http method.
        :return: Page ressource, All loaded ressources.
        """
        body = QtCore.QByteArray()
        try:
            method = getattr(QNetworkAccessManager,
                "%sOperation" % method.capitalize())
        except AttributeError:
            raise Exception("Invalid http method %s" % method)
        request = QNetworkRequest(QtCore.QUrl(address))
        request.setRawHeader("User-Agent", self.user_agent)
        self.main_frame.load(request, method, body)
        self.loaded = False
        return self.wait_for_page_loaded()

    class prompt:
        """Statement that tells Ghost how to deal with javascript prompt().
        """
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            Ghost.prompt_expected = self.value

        def __exit__(self, type, value, traceback):
            Ghost.prompt_expected = None

    def set_viewport_size(self, width, height):
        """Sets the page viewport size.

        :param width: An integer that sets width pixel count.
        :param height: An integer that sets height pixel count.
        """
        self.page.setViewportSize(QtCore.QSize(width, height))

    def wait_for_alert(self):
        """Waits for main frame alert().
        """
        self._wait_for(lambda: Ghost.alert is not None,
            'User has not been alerted.')
        msg = Ghost.alert
        Ghost.alert = None
        return msg, self._release_last_ressources()

    def wait_for_page_loaded(self):
        """Waits until page is loaded, assumed that a page as been requested.
        """
        self._wait_for(lambda: self.loaded,
            'Unable to load requested page')
        ressources = self._release_last_ressources()
        page = None
        for ressource in ressources:
            if not int(ressource.http_status / 100) == 3:
                # Assumed that current ressource is the first non redirect
                page = ressource
        return page, ressources

    def wait_for_selector(self, selector):
        """Waits until selector match an element on the frame.

        :param selector: The selector to wait for.
        """
        self._wait_for(lambda: self.exists(selector),
            'Can\'t find element matching "%s"' % selector)
        return True, self._release_last_ressources()

    def wait_for_text(self, text):
        """Waits until given text appear on main frame.

        :param text: The text to wait for.
        """
        self._wait_for(lambda: text in self.content,
            'Can\'t find "%s" in current frame' % text)
        return True, self._release_last_ressources()

    def _release_last_ressources(self):
        """Releases last loaded ressources.

        :return: The released ressources.
        """
        last_ressources = self.http_ressources
        self.http_ressources = []
        return last_ressources

    def _page_loaded(self):
        """Called back when page is loaded.
        """
        self.loaded = True

    def _page_load_started(self):
        """Called back when page load started.
        """
        self.loaded = False

    def _request_ended(self, res):
        """Adds an HttpRessource object to http_ressources.

        :param res: The request result.
        """
        self.http_ressources.append(HttpRessource(res))

    def _wait_for(self, condition, timeout_message):
        """Waits until condition is True.

        :param condition: A callable that returns the condition.
        :param timeout_message: The exception message on timeout.
        """
        started_at = time.time()
        while not condition():
            if time.time() > (started_at + self.wait_timeout):
                raise Exception(timeout_message)
            time.sleep(0.01)
            self.app.processEvents()
