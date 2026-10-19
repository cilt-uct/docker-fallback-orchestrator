"""HTTP status codes used by the API, and when reading Opencast's responses.

Values come from the stdlib's http.HTTPStatus so nothing here is a hand-typed
number; names follow the HTTP_<code>_<NAME> convention of fastapi.status. Add
a code here before using it rather than writing the number inline.
"""

from http import HTTPStatus

HTTP_200_OK = HTTPStatus.OK.value
HTTP_201_CREATED = HTTPStatus.CREATED.value
HTTP_204_NO_CONTENT = HTTPStatus.NO_CONTENT.value

HTTP_404_NOT_FOUND = HTTPStatus.NOT_FOUND.value
HTTP_409_CONFLICT = HTTPStatus.CONFLICT.value

HTTP_500_INTERNAL_SERVER_ERROR = HTTPStatus.INTERNAL_SERVER_ERROR.value
HTTP_502_BAD_GATEWAY = HTTPStatus.BAD_GATEWAY.value
HTTP_503_SERVICE_UNAVAILABLE = HTTPStatus.SERVICE_UNAVAILABLE.value
HTTP_504_GATEWAY_TIMEOUT = HTTPStatus.GATEWAY_TIMEOUT.value
