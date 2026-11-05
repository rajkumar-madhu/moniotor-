from django.utils.cache import add_never_cache_headers

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

class NoCacheMiddleware:
    """
    This middleware adds 'no-cache' headers to every response to disable
    browser caching for all pages.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        add_never_cache_headers(response)  # Add no-cache headers to the response
        return response


class NoCacheStaticFilesMiddleware(MiddlewareMixin):
    """
    Middleware that disables caching for all static files.
    This middleware checks if the request is for static files and
    sets the appropriate headers to prevent browser caching.
    """
    def process_response(self, request, response):
        # Check if the request is for static files
        if request.path.startswith(settings.STATIC_URL):
            response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'
        return response