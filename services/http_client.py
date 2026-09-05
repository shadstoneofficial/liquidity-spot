import requests
from flask import current_app, has_app_context


class ExternalHTTPDisabled(requests.RequestException):
    pass


def request(method, url, **kwargs):
    """Send external HTTP only when the active app configuration permits it."""
    if has_app_context() and not current_app.config.get('ALLOW_EXTERNAL_HTTP', True):
        raise ExternalHTTPDisabled('External HTTP is disabled by the application configuration.')
    return requests.request(method, url, **kwargs)


def get(url, **kwargs):
    return request('GET', url, **kwargs)


def post(url, **kwargs):
    return request('POST', url, **kwargs)
