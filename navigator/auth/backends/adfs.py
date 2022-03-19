"""ADFSAuth.

Description: Backend Authentication/Authorization using Okta Service.
"""
from numpy import True_
import rapidjson
import logging
import asyncio
import base64
from aiohttp import web, hdrs
from .external import ExternalAuth
from typing import List, Dict, Any
import jwt
from .jwksutils import rsa_pem_from_jwk, jwks
# needed by ADFS
from xml.etree import ElementTree
import requests
import requests.adapters
from urllib3.util.retry import Retry
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.x509 import load_der_x509_certificate

from navigator.exceptions import (
    NavException,
    UserDoesntExists,
    InvalidAuth,
    FailedAuth
)
from navigator.conf import (
    ADFS_SERVER,
    ADFS_CLIENT_ID,
    ADFS_RELYING_PARTY_ID,
    ADFS_TENANT_ID,
    ADFS_RESOURCE,
    ADFS_AUDIENCE,
    ADFS_ISSUER,
    USERNAME_CLAIM,
    GROUP_CLAIM,
    ADFS_CLAIM_MAPPING,
    ADFS_CALLBACK_REDIRECT_URL,
    ADFS_LOGIN_REDIRECT_URL,
    AZURE_AD_SERVER
)


def load_certs(response):
    """Extract token signing certificates."""
    xml_tree = ElementTree.fromstring(response.content)
    cert_nodes = xml_tree.findall(
            "./{urn:oasis:names:tc:SAML:2.0:metadata}RoleDescriptor"
            "[@{http://www.w3.org/2001/XMLSchema-instance}type='fed:SecurityTokenServiceType']"
            "/{urn:oasis:names:tc:SAML:2.0:metadata}KeyDescriptor[@use='signing']"
            "/{http://www.w3.org/2000/09/xmldsig#}KeyInfo"
            "/{http://www.w3.org/2000/09/xmldsig#}X509Data"
            "/{http://www.w3.org/2000/09/xmldsig#}X509Certificate"
    )
    signing_certificates = [node.text for node in cert_nodes]
    new_keys = []
    for cert in signing_certificates:
        logging.debug("Loading public key from certificate: %s", cert)
        cert_obj = load_der_x509_certificate(
            base64.b64decode(cert), backend
        )
        new_keys.append(
            cert_obj.public_key()
        )
    return new_keys

def get_kid(token):
    headers = jwt.get_unverified_header(token)
    print('HEADERS: ', headers)
    if not headers:
        raise Exception('missing headers')
    try:
        return headers['kid']
    except KeyError:
        raise Exception('missing kid')

def get_jwk(kid):
    for jwk in jwks.get('keys'):
        if jwk.get('kid') == kid:
            return jwk
    raise Exception('kid not recognized')

def get_public_key(token):
    return rsa_pem_from_jwk(
        get_jwk(
            get_kid(token)
    )
)

class ADFSAuth(ExternalAuth):
    """ADFSAuth.

    Description: Authentication Backend using
    Active Directory Federation Service (ADFS).
    """
    _service_name: str = "adfs"
    user_attribute: str = "user"
    username_attribute: str = "username"
    pwd_atrribute: str = "password"
    version = 'v1.0'
    _user_mapping: Dict = {
        'email': 'upn',
        'given_name': 'given_name',
        'family_name': 'family_name',
        'name': 'display-Name',
        'groups': "group"
    }

    def configure(self, app, router, handler):
        super(ADFSAuth, self).configure(app, router, handler)
        # URIs:
        if ADFS_TENANT_ID:
            self.server = AZURE_AD_SERVER
            self.tenant_id = ADFS_TENANT_ID
            self.username_claim = "upn"
            self.groups_claim = "groups"
            self.claim_mapping = ADFS_CLAIM_MAPPING
        else:
            self.server = ADFS_SERVER
            self.tenant_id = 'adfs'
            self.username_claim = USERNAME_CLAIM
            self.groups_claim = GROUP_CLAIM
            self.claim_mapping = ADFS_CLAIM_MAPPING
        
        self.base_uri = f"https:://{self.server}/"
        self.end_session_endpoint = f"https://{self.server}/{self.tenant_id}/ls/?wa=wsignout1.0"
        self._issuer = f"https://{self.server}/{self.tenant_id}/services/trust"
        self.authorize_uri = f"https://{self.server}/{self.tenant_id}/oauth2/authorize/"
        self._token_uri = f"https://{self.server}/{self.tenant_id}/oauth2/token"
        self.userinfo_uri = "https://graph.microsoft.com/v1.0/me"

        if ADFS_LOGIN_REDIRECT_URL is not None:
            login = ADFS_LOGIN_REDIRECT_URL
        else:
            login = "/api/v1/auth/{}/".format(self._service_name)

        if ADFS_CALLBACK_REDIRECT_URL is not None:
            callback = ADFS_CALLBACK_REDIRECT_URL
            self.redirect_uri = "{domain}" + callback
        else:
            callback = "/auth/{}/callback/".format(self._service_name)
        # Login and Redirect Routes:
        router.add_route(
            "*",
            login,
            self.authenticate,
            name="{}_login".format(self._service_name)
        )
        # finish login (callback)
        router.add_route(
            "*",
            callback,
            self.auth_callback,
            name="{}_callback_login".format(self._service_name)
        )
        

    async def authenticate(self, request: web.Request):
        """ Authenticate, refresh or return the user credentials.

        Description: This function returns the ADFS authorization URL.
        """
        domain_url = self.get_domain(request)        
        self.redirect_uri = self.redirect_uri.format(domain=domain_url, service=self._service_name)
        print('REDIRECT: ', self.redirect_uri)
        try:
            self.state = base64.urlsafe_b64encode(self.redirect_uri.encode()).decode()
            query_params = {
                "client_id": ADFS_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "resource": ADFS_RESOURCE,
                "response_mode": "query",
                "state": self.state,
                "scope": 'openid'
            }
            login_url = "{base_uri}?{query_params}".format(
                base_uri=self.authorize_uri,
                query_params=requests.compat.urlencode(query_params)
            )
            # Step A: redirect
            return self.redirect(login_url)
        except Exception as err:
            print('HERE: ', err)
            raise NavException(
                f"Client doesn't have info for ADFS Authentication: {err}"
            )

    async def auth_callback(self, request: web.Request):     
        domain_url = self.get_domain(request)        
        self.redirect_uri = self.redirect_uri.format(domain=domain_url, service=self._service_name)
        try:
            auth_response = dict(request.rel_url.query.items())
            print(auth_response)
            authorization_code = auth_response['code']
            state = auth_response['state']
            request_id = auth_response['client-request-id']
        except Exception as err:
            print(err)
            raise NavException(
                f"ADFS: Invalid Callback response: {err}"
            )
        print(authorization_code, state, request_id)
        logging.debug("Received authorization token: " + authorization_code)
        # getting an Access Token
        query_params = {
            "code": authorization_code,
            "client_id": ADFS_CLIENT_ID,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "scope": "https://graph.microsoft.com/.default"
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        try:
            exchange = await self.post(self._token_uri, data=query_params, headers=headers)
            if 'error' in exchange:
                error = exchange.get('error')
                desc = exchange.get('error_description')
                message = f"Azure {error}: {desc}¡"
                logging.exception(message)
                raise web.HTTPForbidden(
                    reason=message
                )
            else:
                ## processing the exchange response:
                access_token = exchange["access_token"]
                token_type = exchange["token_type"] # ex: Bearer
                id_token = exchange["id_token"]
                logging.debug(f"Received access token: {access_token}")
                # getting user information:
                options = {
                    'verify_signature': True,
                    'verify_exp': True,
                    'verify_nbf': True,
                    'verify_iat': True,
                    'verify_aud': True,
                    'verify_iss': True,
                    'require_exp': False,
                    'require_iat': False,
                    'require_nbf': False
                }
                public_key = get_public_key(access_token)
                # Validate token and extract claims
                data = jwt.decode(
                    access_token,
                    key=public_key,
                    algorithms=['RS256', 'RS384', 'RS512'],
                    verify=True,
                    audience=ADFS_AUDIENCE,
                    issuer=ADFS_ISSUER,
                    options=options,
                )
                print('CLAIMS: ', data)
                try:
                    # build user information:
                    userdata, uid = self.build_user_info(data)
                    userdata['id_token'] = id_token
                    data = await self.create_user(request, uid, userdata, access_token)
                except Exception as err:
                    logging.exception(f'ADFS: Error getting User information: {err}')
                    raise web.HTTPForbidden(
                        reason=f"ADFS: Error with User Information: {err}"
                    )
        except Exception as err:
            raise web.HTTPForbidden(
                reason=f"ADFS: Invalid Response from Server {err}."
            )

    async def logout(self, request):
        # first: removing the existing session
        # second: redirect to SSO logout
        logging.debug(f'ADFS LOGOUT URI: {self.end_session_endpoint}')
        return web.HTTPFound(self.end_session_endpoint)

    async def finish_logout(self, request):
        pass

    async def check_credentials(self, request):
        """ Authentication and create a session."""
        return True
