from .exceptions import UnknownKid
from .jwtse import verify_jws, unpad_jwt_head, unpad_jwt_payload

import asyncio
import json
import logging
from .http_client import http_get


OIDCFED_FEDERATION_WELLKNOWN_URL = ".well-known/openid-federation"
logger = logging.getLogger(__name__)


def jwks_from_jwks_uri(jwks_uri: str, httpc_params: dict = {}) -> list:
    return [json.loads(asyncio.run(http_get([jwks_uri], httpc_params)))]


def get_jwks(jwt_payload: dict, httpc_params: dict = {}):
    return (
        jwt_payload.get("jwks")
        # TODO: we must only support signed_jwks_uri
        # or jwks_from_jwks_uri(jwks_uri, httpc_params)
    )


def get_entity_statements(urls: list, httpc_params: dict = {}) -> list:
    """
    Fetches an entity statement/configuration
    """
    if isinstance(urls, str):
        urls = [urls]
    for url in urls:
        logger.info(f"Starting Entity Statement Request for {url}")
    responses = asyncio.run(http_get(urls, httpc_params))
    return responses


def get_entity_configurations(subjects: list, httpc_params: dict = {}):
    if isinstance(subjects, str):
        subjects = [subjects]
    urls = []
    for subject in subjects:
        if subject[-1] != "/":
            subject = f"{subject}/"
        url = f"{subject}{OIDCFED_FEDERATION_WELLKNOWN_URL}"
        urls.append(url)
        logger.info(f"Starting Entity Configuration Request for {url}")
    return asyncio.run(http_get(urls, httpc_params))


class EntityConfiguration:
    """
    The self issued/signed statement of a federation entity
    """

    def __init__(
        self,
        jwt: str,
        httpc_params: dict = {},
        filter_by_allowed_trust_marks: list = [],
        trust_anchors_entity_confs: dict = [],
        trust_mark_issuers_entity_confs: dict = [],
    ):
        self.jwt = jwt
        self.header = unpad_jwt_head(jwt)
        self.payload = unpad_jwt_payload(jwt)
        self.sub = self.payload["sub"]
        self.jwks = get_jwks(self.payload, httpc_params)
        self.kids = [i.get("kid") for i in self.jwks]
        self.httpc_params = httpc_params

        self.filter_by_allowed_trust_marks = filter_by_allowed_trust_marks
        self.trust_anchors_entity_confs = trust_anchors_entity_confs
        self.trust_mark_issuers_entity_confs = trust_mark_issuers_entity_confs

        # a dict with sup_sub : superior entity configuration
        self.verified_superiors = {}
        # as previous but with superiors with invalid entity configurations
        self.failed_superiors = {}

        # a dict with sup_sub : entity statement issued for self
        self.verified_by_superiors = {}
        self.failed_by_superiors = {}

        self.is_valid = False

    def validate_by_itself(self) -> bool:
        """
        validates the entity configuration by it self
        """
        # TODO: pydantic entity configuration validation here
        if self.header.get("kid") not in self.kids:
            raise UnknownKid(f"{self.header.get('kid')} not found in {self.jwks}")
        # verify signature
        verify_jws(self.jwt, self.jwks[self.kids.index(self.header["kid"])])
        self.is_valid = True
        return True

    def get_valid_trust_marks(self) -> bool:
        """
        validate the entity configuration ony if marked by a well known
        trust mark, issued by a trusted issuer
        """
        # if not self.filter_by_allowed_trust_marks:
        # return True
        # TODO
        raise NotImplementedError()

    def get_superiors(
        self, authority_hints: list = [], max_authority_hints: int = 0
    ) -> dict:
        """
        get superiors entity configurations
        """
        # apply limits if defined
        authority_hints = authority_hints or self.payload["authority_hints"]
        if (
            max_authority_hints
            and authority_hints != authority_hints[:max_authority_hints]
        ):
            logger.warning(
                f"Found {len(authority_hints)} but "
                f"authority maximum hints is set to {max_authority_hints}. "
                "the following authorities will be ignored: "
                f"{', '.join(authority_hints[max_authority_hints:])}"
            )
            authority_hints = authority_hints[:max_authority_hints]

        jwts = get_entity_statements(authority_hints, self.httpc_params)
        for jwt in jwts:
            ec = self.__class__(jwt, httpc_params=self.httpc_params)

            if ec.validate_by_itself():
                target = self.verified_superiors
            else:
                target = self.failed_superiors

            target[ec.payload["sub"]] = ec
        return self.verified_superiors

    def validate_descendant_statement(self, jwt: str) -> bool:
        """
        jwt is a descendant entity statement issued by self
        """
        # TODO: pydantic entity configuration validation here
        header = unpad_jwt_head(jwt)
        unpad_jwt_payload(jwt)
        if header.get("kid") not in self.kids:
            raise UnknownKid(f"{self.header.get('kid')} not found in {self.jwks}")
        # verify signature
        verify_jws(jwt, self.jwks[self.kids.index(header["kid"])])
        return True

    def validate_by_superior_statement(self, jwt: str, ec) -> bool:
        """
        jwt is a statement issued by a superior

        this method validates self with the jwks contained in statement
        of the superior
        """
        is_valid = None
        try:
            payload = unpad_jwt_payload(jwt)
            ec.validate_by_itself()
            ec.validate_descendant_statement(jwt)
            _jwks = get_jwks(payload, self.httpc_params)
            _kids = [i.get("kid") for i in _jwks]
            verify_jws(self.jwt, _jwks[_kids.index(self.header["kid"])])
            is_valid = True
        except Exception:
            is_valid = False

        if is_valid:
            target = self.verified_superiors
        else:
            target = self.failed_superiors

        target[payload["iss"]] = payload
        return is_valid

    def validate_by_superiors(
        self,
        superiors_entity_configurations: dict = {},
    ):  # -> dict[str, EntityConfiguration]:
        """
        validates the entity configuration with the entity statements
        issued by its superiors

        this methods create self.verified_superiors and failed ones
        and self.verified_by_superiors and failed ones
        """

        for ec in superiors_entity_configurations:
            try:
                # get superior fetch url
                fetch_api_url = ec.payload["metadata"]["federation_entity"][
                    "federation_api_endpoint"
                ]
            except KeyError:
                logger.warning(
                    "Missing federation_api_endpoint in federation_entity metadata "
                    f"for {self.sub} by {ec.sub}."
                )
                self.invalid_superiors[ec.sub] = None
                continue

            else:
                jwts = get_entity_statements([fetch_api_url], self.httpc_params)
                jwt = jwts[0]
                self.verified_by_superiors[
                    ec.sub
                ] = self.validate_by_superior_statement(jwt, ec)

        return self.verified_by_superiors

    def __str__(self) -> str:
        return f"{self.sub} valid {self.is_valid}"