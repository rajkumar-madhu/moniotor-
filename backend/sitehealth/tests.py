from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase

from sitehealth.views import sitehealth


def _site_obj(**overrides):
    fields = {
        'sitename': 'site-a',
        'redis_host': '10.0.0.10',
        'redis_port': '6379',
        'le_url': 'https://site-a.example/',
        'entity_host': 'entity.example',
        'entity_port': '443',
        'is_URLSecured': True,
    }
    fields.update(overrides)
    return type('SiteObj', (), fields)()


class SiteHealthViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch('sitehealth.views.SiteModel')
    @patch('sitehealth.views.Redis')
    def test_sitehealth_returns_json_content_type(self, redis_cls, site_model_cls):
        redis_instance = redis_cls.return_value
        redis_instance.getSiteHealthNew.return_value = {'ok': True}
        site_model_cls.objects.filter.return_value.first.return_value = None

        response = sitehealth(self.factory.get('/sitehealth/overall?sitename=demo'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertIn('"ok": true', response.content.decode().lower())

    @patch('sitehealth.views.SiteModel')
    @patch('sitehealth.views.Redis')
    def test_sitehealth_uses_requested_sitename_as_is(self, redis_cls, site_model_cls):
        redis_instance = redis_cls.return_value
        redis_instance.getSiteHealthNew.return_value = {'ok': True}
        site_model_cls.objects.filter.return_value.first.return_value = None

        response = sitehealth(self.factory.get('/sitehealth/overall?sitename=site-a'))

        self.assertEqual(response.status_code, 200)
        redis_instance.getSiteHealthNew.assert_called_once_with(site='site-a')
        site_model_cls.objects.filter.assert_called_once_with(sitename='site-a')

    @patch('sitehealth.views.requests.get')
    @patch('sitehealth.views.Redis')
    @patch('sitehealth.views.SiteModel')
    @patch('sitehealth.views.os.getenv')
    def test_sitehealth_uses_remote_api_when_redis_is_not_local(
        self, getenv, site_model_cls, redis_cls, requests_get,
    ):
        getenv.side_effect = lambda key, default='': {
            'REDIS_HOST': 'aggregator-redis.local',
        }.get(key, default)

        site_model_cls.objects.filter.return_value.first.return_value = _site_obj()

        remote_response = MagicMock()
        remote_response.json.return_value = {
            'code': 200,
            'data': {'bod': 1, 'eod': 0, 'adp': 0, 'entity': 0},
        }
        remote_response.raise_for_status.return_value = None
        requests_get.return_value = remote_response

        response = sitehealth(self.factory.get('/sitehealth/overall?sitename=site-a'))

        self.assertEqual(response.status_code, 200)
        self.assertIn('"bod": 1', response.content.decode())
        redis_cls.assert_not_called()
        requests_get.assert_called_once()
        _, kwargs = requests_get.call_args
        self.assertEqual(kwargs['params']['sitename'], 'site-a')

    @patch('sitehealth.views.requests.get')
    @patch('sitehealth.views.Redis')
    @patch('sitehealth.views.SiteModel')
    @patch('sitehealth.views.os.getenv')
    def test_sitehealth_uses_lesite_redis_and_neo4j_when_local(
        self, getenv, site_model_cls, redis_cls, requests_get,
    ):
        getenv.side_effect = lambda key, default='': {
            'REDIS_HOST': '10.0.0.10',
        }.get(key, default)

        site_model_cls.objects.filter.return_value.first.return_value = _site_obj()
        redis_cls.return_value.getSiteHealthNew.return_value = {'bod': 1}

        response = sitehealth(self.factory.get('/sitehealth/overall?sitename=site-a'))

        self.assertEqual(response.status_code, 200)
        redis_cls.assert_called_once_with(host='10.0.0.10', port='6379')
        redis_cls.return_value.getSiteHealthNew.assert_called_once_with(
            site='site-a',
            entity_host='entity.example',
            entity_port='443',
            entity_secure=True,
        )
        requests_get.assert_not_called()


class RemoteSitehealthServiceTokenTests(SimpleTestCase):
    """Sister LE deployments answer 401 unless the shared service token rides
    along on the server-to-server hop."""

    def _remote(self, body, status_code=200):
        remote = MagicMock()
        remote.status_code = status_code
        remote.json.return_value = body
        return remote

    @patch('sitehealth.views.requests.get')
    @patch('sitehealth.views.get_app_secret', return_value='s3cret')
    def test_remote_hop_sends_service_token_and_proxy_marker(self, _secret, requests_get):
        from sitehealth.views import (
            PROXY_HEADER, SERVICE_TOKEN_HEADER, _fetch_remote_sitehealth,
        )
        requests_get.return_value = self._remote({'code': 200, 'data': {'bod': 1}})

        payload = _fetch_remote_sitehealth(_site_obj(), 'site-a')

        self.assertEqual(payload['data'], {'bod': 1})
        self.assertEqual(requests_get.call_args.args[0], 'https://site-a.example/sitehealth/overall')
        kwargs = requests_get.call_args.kwargs
        self.assertEqual(kwargs['headers'][SERVICE_TOKEN_HEADER], 's3cret')
        self.assertEqual(kwargs['headers'][PROXY_HEADER], '1')
        self.assertFalse(kwargs['allow_redirects'])

    @patch('sitehealth.views.requests.get')
    @patch('sitehealth.views.get_app_secret', return_value='')
    def test_remote_hop_without_token_omits_header(self, _secret, requests_get):
        from sitehealth.views import (
            PROXY_HEADER, SERVICE_TOKEN_HEADER, _fetch_remote_sitehealth,
        )
        requests_get.return_value = self._remote({'code': 200, 'data': {}})

        _fetch_remote_sitehealth(_site_obj(), 'site-a')

        headers = requests_get.call_args.kwargs['headers']
        self.assertNotIn(SERVICE_TOKEN_HEADER, headers)
        self.assertEqual(headers[PROXY_HEADER], '1')

    @patch('sitehealth.views.requests.get')
    @patch('sitehealth.views.get_app_secret', return_value='s3cret')
    def test_remote_redirect_is_refused(self, _secret, requests_get):
        from sitehealth.views import _fetch_remote_sitehealth
        remote = self._remote({}, status_code=302)
        remote.headers = {'Location': 'https://elsewhere.example/'}
        requests_get.return_value = remote

        with self.assertRaises(Exception):
            _fetch_remote_sitehealth(_site_obj(), 'site-a')

    @patch('sitehealth.views.requests.get')
    @patch('sitehealth.views.Redis')
    @patch('sitehealth.views.SiteModel')
    @patch('sitehealth.views.os.getenv')
    def test_remote_site_failure_returns_503_without_touching_its_redis(
        self, getenv, site_model_cls, redis_cls, requests_get,
    ):
        """One dead remote site must fail fast and alone: no direct Redis
        connect to the other cluster (which hangs ~60s and pins a worker)."""
        getenv.side_effect = lambda key, default='': {
            'REDIS_HOST': 'aggregator-redis.local',
        }.get(key, default)
        site_model_cls.objects.filter.return_value.first.return_value = _site_obj()
        requests_get.side_effect = Exception('401 Client Error')

        response = sitehealth(RequestFactory().get('/sitehealth/overall?sitename=site-a'))

        self.assertEqual(response.status_code, 503)
        self.assertIn('"code": 503', response.content.decode())
        redis_cls.assert_not_called()

    def test_redis_client_is_created_with_bounded_timeouts(self):
        from lib.LinkedEyeRedis import Redis
        site = _site_obj()
        with patch('redis.StrictRedis') as strict_redis:
            Redis(host=site.redis_host, port=site.redis_port)
        kwargs = strict_redis.call_args.kwargs
        self.assertGreater(kwargs['socket_connect_timeout'], 0)
        self.assertLessEqual(kwargs['socket_connect_timeout'], 5)
        self.assertGreater(kwargs['socket_timeout'], 0)
        self.assertFalse(kwargs['retry_on_timeout'])
        if 'retry' in kwargs:
            # redis-py >= 4: the implicit 10-attempt backoff must be pinned off.
            self.assertEqual(kwargs['retry']._retries, 0)

    def test_service_token_is_a_canonical_vault_key(self):
        from lib.LinkedEyeVault.AppSecrets import APP_SECRET_KEYS
        self.assertIn('LE_SERVICE_TOKEN', APP_SECRET_KEYS)
