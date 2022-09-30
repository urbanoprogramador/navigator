import asyncio
import datetime
import traceback
from json.decoder import JSONDecodeError
from typing import Any
from collections.abc import Callable
from urllib import parse
import aiohttp_cors
from aiohttp import web
from aiohttp.abc import AbstractView
# from aiohttp.web import Response, StreamResponse
from aiohttp.web_exceptions import (
    # HTTPClientError,
    # HTTPInternalServerError,
    HTTPMethodNotAllowed,
    HTTPNoContent,
    HTTPNotImplemented,
    # HTTPUnauthorized,
)
from aiohttp_cors import CorsViewMixin

from asyncdb.models import Model
from asyncdb.exceptions import (
    ProviderError,
    DriverError,
    NoDataFound
)
from navconfig.logging import logging, loglevel
from navigator.utils.functions import SafeDict
from navigator.libs.json import JSONContent


class BaseHandler(CorsViewMixin):
    _config = None
    _mem = None
    _now = None
    _loop = None
    logger: logging.Logger
    _lasterr = None
    _allowed = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

    cors_config = {
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            allow_headers="*",
        )
    }

    def __init__(self, *args, **kwargs):
        CorsViewMixin.__init__(self)
        self._now = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        self._loop = asyncio.get_event_loop()
        self._json: Callable = JSONContent()
        self.logger = logging.getLogger('navigator')
        self.logger.setLevel(loglevel)
        self.post_init(self, *args, **kwargs)

    def post_init(self, *args, **kwargs):
        pass

    def now(self):
        return self._now

    def log(self, message: str):
        self.logger.info(message)

    # function returns
    def no_content(
        self,
        request: web.Request = None,
        headers: dict = None,
        content_type: str = "application/json",
    ) -> web.Response:
        if not headers:
            headers = {}
        if not request:
            request = self.request
        response = HTTPNoContent(content_type=content_type)
        response.headers["Pragma"] = "no-cache"
        for header, value in headers.items():
            response.headers[header] = value
        return response

    def response(
        self,
        request: web.Request = None,
        response: str = "",
        state: int = 200,
        headers: dict = None,
        **kwargs,
    ) -> web.Response:
        if not headers: # TODO: set to default headers.
            headers = {}
        if not request:
            request = self.request
        args = {"status": state, "content_type": "application/json", **kwargs}
        if isinstance(response, dict):
            args["text"] = self._json.dumps(response)
        else:
            args["body"] = response
        obj = web.Response(**args)
        for header, value in headers.items():
            obj.headers[header] = value
        return obj

    def json_response(self, response: dict = None, headers: dict = None, state: int = 200, cls: Callable = None):
        if not headers: # TODO: set to default headers.
            headers = {}
        jsonfn = self._json.dumps
        obj = web.json_response(response, status=state, headers=headers, dumps=jsonfn)
        for header, value in headers.items():
            obj.headers[header] = value
        return obj

    def critical(
        self,
        request: web.Request = None,
        exception: Exception = None,
        traceback: str = None,
        state: int = 500,
        headers: dict = None,
        **kwargs,
    ) -> web.Response:
        # TODO: process the exception object
        if not headers: # TODO: set to default headers.
            headers = {}
        if not request:
            request = self.request
        response_obj = {
            "status": "Failed",
            "reason": str(exception),
            "stacktrace": traceback,
        }
        args = {
            "text": self._json.dumps(response_obj),
            "reason": "Server Error",
            "content_type": "application/json",
            **kwargs,
        }
        if state == 500:  # bad request
            obj = web.HTTPInternalServerError(**args)
        else:
            obj = web.HTTPServerError(**args)
        for header, value in headers.items():
            obj.headers[header] = value
        return obj

    def error(
        self,
        request: web.Request = None,
        response: dict = None,
        exception: Exception = None,
        state: int = 400,
        headers: dict = None,
        **kwargs,
    ) -> web.Response:
        if not headers: # TODO: set to default headers.
            headers = {}
        # TODO: process the exception object
        response_obj = {"status": "Failed"}
        if not request:
            request = self.request
        if exception:
            response_obj["reason"] = str(exception)
        args = {**kwargs}
        if isinstance(response, dict):
            response_obj = {**response_obj, **response}
            args["content_type"] = "application/json"
            args["text"] = self._json.dumps(response_obj)
        else:
            args["body"] = response
        # defining the error
        if state == 400:  # bad request
            obj = web.HTTPBadRequest(**args)
        elif state == 401:  # unauthorized
            obj = web.HTTPUnauthorized(**args)
        elif state == 403:  # forbidden
            obj = web.HTTPForbidden(**args)
        elif state == 404:  # not found
            obj = web.HTTPNotFound(**args)
        elif state == 406:
            obj = web.HTTPNotAcceptable(**args)
        elif state == 412:
            obj = web.HTTPPreconditionFailed(**args)
        elif state == 428:
            obj = web.HTTPPreconditionRequired(**args)
        else:
            obj = web.HTTPBadRequest(**args)
        for header, value in headers.items():
            obj.headers[header] = value
        return obj

    def not_implemented(
        self, request: web.Request, response: dict = None, headers: dict = None, **kwargs
    ) -> web.Response:
        args = {
            "text": self._json.dumps(response),
            "reason": "Method not Implemented",
            "content_type": "application/json",
            **kwargs,
        }
        response = HTTPNotImplemented(**args)
        for header, value in headers.items():
            response.headers[header] = value
        return response

    def not_allowed(
        self,
        request: web.Request = None,
        response: dict = None,
        headers: dict = None,
        allowed: dict = None,
        **kwargs,
    ) -> web.Response:
        if not request:
            request = self.request
        if not allowed:
            allow = self._allowed
        else:
            allow = allowed
        args = {
            "method": request.method,
            "text": self._json.dumps(response),
            "reason": "Method not Allowed",
            "content_type": "application/json",
            "allowed_methods": allow,
            **kwargs,
        }
        if allowed:
            headers["Allow"] = ",".join(allow)
        else:
            headers["Allow"] = ",".join(allow)
        obj = HTTPMethodNotAllowed(**args)
        for header, value in headers.items():
            obj.headers[header] = value
        return obj

    def query_parameters(self, request: web.Request) -> dict:
        return {key: val for (key, val) in request.query.items()}

    async def body(self, request: web.Request) -> str:
        body = None
        try:
            if request.body_exists:
                body = await request.read()
                body = body.decode("ascii")
        except Exception: # pylint: disable=W0703
            pass
        finally:
            return body # pylint: disable=W0150

    async def json_data(self, request: web.Request = None):
        if not request:
            request = self.request
        return await request.json()

    def match_parameters(self, request: web.Request = None) -> dict:
        params = {}
        if not request:
            request = self.request
        for arg in request.match_info:
            try:
                val = request.match_info.get(arg)
                object.__setattr__(self, arg, val)
                params[arg] = val
            except AttributeError:
                pass
        return params

    def get_arguments(self, request: web.Request = None) -> dict:
        params = {}
        if not request:
            rq = self.request
        else:
            rq = request
        for arg in rq.match_info:
            try:
                val = rq.match_info.get(arg)
                object.__setattr__(self, arg, val)
                params[arg] = val
            except AttributeError:
                pass
        qry = {}
        try:
            qry = {key: val for (key, val) in rq.rel_url.query.items()}
        except Exception as err:
            print(err)
        params = {**params, **qry}
        return params

    get_args = get_arguments

    async def data(self, request: web.Request = None) -> dict:
        # only get post Data
        params = {}
        if not request:
            request = self.request
        try:
            params = await request.json()
        except JSONDecodeError as err:
            logging.debug(f"Invalid POST DATA: {err!s}")
        # if any, mix with match_info data:
        for arg in request.match_info:
            try:
                val = request.match_info.get(arg)
                # object.__setattr__(self, arg, val)
                params[arg] = val
            except AttributeError:
                pass
        return params


class BaseView(web.View, BaseHandler, AbstractView):
    # _mcache: Any = None
    _connection: Any = None
    _redis: Any = None

    cors_config = {
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            allow_headers="*",
        )
    }

    def __init__(self, request, *args, **kwargs):
        AbstractView.__init__(self, request)
        BaseHandler.__init__(self, *args, **kwargs)
        CorsViewMixin.__init__(self)
        self._request = request

    async def connection(self):
        return self._connection

    async def connect(self, request):
        self._connection = await request.app["database"].acquire()
        try:
            self._redis = request.app["redis"]
        except (ProviderError, DriverError) as ex:
            raise RuntimeError(
                f'Error connecting to Database: {ex}'
            ) from ex
        except Exception as err:
            self.logger.exception(err, stack_info=True)

    async def close(self):
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def post_data(self) -> dict:
        params = {}
        if self.request.headers.get("Content-Type") == "application/json":
            try:
                return await self.request.json()
            except JSONDecodeError as ex:
                logging.exception(f'Empty POST Data, {ex}')
                return None
        try:
            params = await self.request.post()
            if not params or len(params) == 0:
                if self.request.body_exists:
                    body = await self.request.read()
                    body = body.decode("ascii")
                    if body:
                        try:
                            params = dict(
                                (k, v if len(v) > 1 else v[0])
                                for k, v in parse.parse_qs(body).items()
                            )
                        except (KeyError, ValueError):
                            pass
        finally:
            return params


class DataView(BaseView):
    async def asyncdb(self, request):
        try:
            pool = request.app["database"]
            if pool.is_connected():
                db = await pool.acquire()
                # TODO: return new build (ex-ORM)
                return db
        except (ProviderError, DriverError) as ex:
            raise Exception(
                f"Error connecting to DB: {ex}"
            ) from ex
        except Exception as err:
            raise Exception(
                f"Error connecting to DB: {err}"
            ) from err

    async def query(self, sql):
        result = None
        if self._connection:
            self._lasterr = None
            try:
                result, error = await self._connection.query(sql)
                if error:
                    print(error)
                    result = None
                    self._lasterr = error
            except Exception as err:
                print(err)
                result = None
                self._lasterr = err
            finally:
                return result

    async def queryrow(self, sql):
        result = None
        if self._connection:
            self._lasterr = None
            try:
                result, error = await self._connection.queryrow(sql)
                if error:
                    result = None
                    self._lasterr = error
            except NoDataFound:
                raise
            except (ProviderError, DriverError) as ex:
                self._lasterr = ex
            except Exception as err:
                self._lasterr = err
                raise Exception(
                    f"Error connecting to DB: {err}"
                ) from err
            finally:
                return result

    async def execute(self, sql):
        result = None
        if self._connection:
            self._lasterr = None
            try:
                result, error = await self._connection.execute(sql)
                if error:
                    result = None
                    self._lasterr = error
            except NoDataFound:
                self._lasterr = None
            except (ProviderError, DriverError) as ex:
                self._lasterr = ex
            except Exception as err:
                self._lasterr = err
                raise Exception(
                    f"Error connecting to DB: {err}"
                ) from err
            finally:
                return result

### Meta-Operations
    def table(self, table):
        try:
            return self._query_raw.format_map(SafeDict(table=table))
        except Exception as e:
            print(e)
            return False

    def fields(self, sentence, fields=None):
        _sql = False
        if not fields:
            _sql = sentence.format_map(SafeDict(fields="*"))
        elif type(fields) == str:
            _sql = sentence.format_map(SafeDict(fields=fields))
        elif type(fields) == list:
            _sql = sentence.format_map(SafeDict(fields=",".join(fields)))
        return _sql

    def where(self, sentence, where):
        sql = ""
        if sentence:
            where_string = ""
            if not where:
                sql = sentence.format_map(SafeDict(where_cond=""))
            elif type(where) == dict:
                where_cond = []
                for key, value in where.items():
                    # print("KEY {}, VAL: {}".format(key, value))
                    if type(value) == str or type(value) == int:
                        if value == "null" or value == "NULL":
                            where_string.append("%s IS NULL" % (key))
                        elif value == "!null" or value == "!NULL":
                            where_string.append("%s IS NOT NULL" % (key))
                        elif key.endswith("!"):
                            where_cond.append("%s != %s" % (key[:-1], value))
                        else:
                            if (
                                type(value) == str
                                and value.startswith("'")
                                and value.endswith("'")
                            ):
                                where_cond.append("%s = %s" % (key, "{}".format(value)))
                            elif type(value) == int:
                                where_cond.append("%s = %s" % (key, "{}".format(value)))
                            else:
                                where_cond.append(
                                    "%s = %s" % (key, "'{}'".format(value))
                                )
                    elif type(value) == bool:
                        val = str(value)
                        where_cond.append("%s = %s" % (key, val))
                    else:
                        val = ",".join(map(str, value))
                        if type(val) == str and "'" not in val:
                            where_cond.append("%s IN (%s)" % (key, "'{}'".format(val)))
                        else:
                            where_cond.append("%s IN (%s)" % (key, val))
                # if 'WHERE ' in sentence:
                #    where_string = ' AND %s' % (' AND '.join(where_cond))
                # else:
                where_string = " WHERE %s" % (" AND ".join(where_cond))
                # print("WHERE cond is %s" % where_string)
                sql = sentence.format_map(SafeDict(where_cond=where_string))
            elif type(where) == str:
                where_string = where
                if not where.startswith("WHERE"):
                    where_string = " WHERE %s" % where
                sql = sentence.format_map(SafeDict(where_cond=where_string))
            else:
                sql = sentence.format_map(SafeDict(where_cond=""))
            del where
            del where_string
            return sql
        else:
            return False

    def limit(self, sentence, limit=1):
        """
        LIMIT
          add limiting to SQL
        """
        if sentence:
            return "{q} LIMIT {limit}".format(q=sentence, limit=limit)
        return self

    def orderby(self, sentence, ordering=[]):
        """
        LIMIT
          add limiting to SQL
        """
        if sentence:
            if type(ordering) == str:
                return "{q} ORDER BY {ordering}".format(q=sentence, ordering=ordering)
            elif type(ordering) == list:
                return "{q} ORDER BY {ordering}".format(
                    q=sentence, ordering=", ".join(ordering)
                )
        return self


async def load_models(app: str, model, tablelist: list):
    pool = app["database"]
    async with await pool.acquire() as conn:
        name = app["name"]
        if isinstance(tablelist, list):
            for table in tablelist:
                try:
                    query = await Model.makeModel(name=table, schema=name, db=conn)
                    model[table] = query
                except Exception as err:
                    logging.error(
                        f"Error loading Model {table}: {err!s}"
                    )
            return model


class ModelView(BaseView):
    """ModelView.

    description: API View using AsyncDB Models.
    tags:
      - Model
      - AsyncDB Model
    parameters:
      - name: model
        in: Model
        type: Model
        required: true
        description: DB Model using asyncdb Model.
    """
    def __init__(self, request, *args, **kwargs):
        self.model: Model = None
        self.models: dict = {}
        super(ModelView, self).__init__(request, *args, **kwargs)
        # getting model associated
        try:
            self.model = self.get_schema()
        except NoDataFound as err:
            raise Exception(err) from err

    def get_schema(self):
        if self.model:
            return self.model
        else:
            # TODO: try to discover from Model Name and model declaration
            # using importlib (from apps.{program}.models import Model)
            try:
                table = self.Meta.tablename
            except Exception as err:
                print(err)
                table = type(self).__name__
                self.Meta.tablename = table
            try:
                return self.models[table]
            except KeyError as err:
                # Model doesn't exists
                raise NoDataFound(
                    f"Model {table} Doesn't Exists"
                ) from err

    async def get_data(self, params, args):
        try:
            if len(params) > 0:
                print("FILTER")
                query = await self.model.filter(**params)
            elif len(args) > 0:
                print("GET")
                query = await self.model.get(**args)
                return query.dict()
            else:
                print("ALL")
                query = await self.model.all()
            if query:
                return [row.dict() for row in query]
            else:
                raise NoDataFound
        except Exception:
            raise

    def model_response(self, response, headers: dict = {}):
        # TODO: check if response is empty
        if not response:
            return self.no_content(headers=headers)
        # return data only
        return self.json_response(response, headers=headers)

    def get_args(self, request: web.Request = None) -> dict:
        params = {}
        if not request:
            rq = self.request
        else:
            rq = request
        for arg in rq.match_info:
            try:
                val = rq.match_info.get(arg)
                object.__setattr__(self, arg, val)
                params[arg] = val
            except AttributeError:
                pass
        return params

    async def get_parameters(self):
        """Get Parameters.

        Get all parameters from URL or from query string.
        """
        try:
            if not self.model.Meta.connection:
                db = await self.request.app["database"].acquire()
                self.model.Meta.connection = db
        except Exception as err:
            raise Exception(err)
        args = self.get_args()
        params = self.query_parameters(self.request)
        return [args, params]

    async def get(self):
        args, params = await self.get_parameters()
        # print(args, params)
        # TODO: check if QueryParameters are in list of columns in Model
        try:
            data = await self.get_data(params, args)
            return self.model_response(data)
        except NoDataFound:
            headers = {
                "X-STATUS": "EMPTY",
                "X-MESSAGE": f"Data on {self.Meta.tablename} not Found",
            }
            return self.no_content(headers=headers)
        except Exception as err:
            print("ERROR ", err)
            return self.critical(request=self.request, exception=err, traceback="")

    async def patch(self):
        """
        patch.
            summary: return the metadata from table or, if we got post
            realizes a partially atomic updated of the query.
        """
        args, _ = await self.get_parameters()
        # try to got post data
        post = await self.json_data()
        if post:
            # trying to update the model
            update = await self.model.update(args, **post)
            if update:
                data = update[0].dict()
                return self.model_response(data)
            else:
                return self.error(
                    response=f"Resource not found: {post}",
                    request=self.request,
                    state=404,
                )
        else:
            try:
                # getting metadata of Model
                qry = self.model(**args)
                data = {}
                for _, field in qry.columns().items():
                    key = field.name
                    _type = field.db_type()
                    default = None
                    if field.default is not None:
                        default = f"{field.default!r}"
                    data[key] = {"type": _type, "default": default}
                return self.model_response(data)
            except Exception as err:
                return self.critical(request=self.request, exception=err, traceback="")

    async def post(self):
        """
        post.
            summary: update (or create) a row in table
        """
        args, _ = await self.get_parameters()
        post = await self.json_data()
        if not post:
            return self.error(
                request=self.request,
                response="Cannot Update row without JSON Data",
                state=406,
            )
        # updating several at the same time:
        if isinstance(post, list):
            # mass-update using arguments:
            try:
                result = self.model.update(args, **post)
                data = [row.dict() for row in result]
                return self.model_response(data)
            except Exception as err:
                trace = traceback.format_exc()
                return self.critical(
                    request=self.request, exception=err, traceback=trace
                )
        if len(args) > 0:
            parameters = {**args, **post}
            try:
                # check if exists first:
                query = await self.model.get(**args)
                if not query:
                    # object doesnt exists, need to be created:
                    result = await self.model.create([parameters])
                    query = await self.model.get(**parameters)
                    data = query.dict()
                    return self.model_response(data)
            except Exception as err:
                print(err)
                return self.error(
                    request=self.request, response=f"Error Saving Data {err!s}"
                )
        # I need to use post data only
        try:
            qry = self.model(**post)
            if qry.is_valid():
                await qry.save()
                query = await qry.fetch(**args)
                data = query.dict()
                return self.model_response(data)
            else:
                return self.error(
                    request=self.request,
                    response=f"Invalid data for Schema {self.Meta.tablename}",
                )
        except Exception as err:
            print(err)
            trace = traceback.format_exc()
            return self.critical(request=self.request, exception=err, traceback=trace)

    async def delete(self):
        """ "
        delete.
           summary: delete a table object
        """
        args, params = await self.get_parameters()
        try:
            result = None
            if len(args) > 0:
                # need to delete one
                result = await self.model.remove(args)
            elif len(params) > 0:
                result = await self.model.remove(params)
        except Exception as err:
            return self.error(
                request=self.request,
                response="Error Deleting Object",
                exception=err,
                state=400,
            )
        if result is not None:
            msg = {"result": result}
            headers = {
                "X-STATUS": "OK",
                "X-MESSAGE": f"Table row was deleted: {self.model!r}",
                "X-TABLE": self.Meta.tablename,
            }
            return self.model_response(msg, headers=headers)
        else:
            headers = {
                "X-STATUS": "Error",
                "X-MESSAGE": f"Row in Table {self.Meta.tablename} not deleted",
            }
            return self.error(
                response=f"Row in Table {self.Meta.tablename} not deleted",
                headers=headers,
                state=404,
            )

    async def put(self):
        """ "
        put.
           summary: insert a row in table
        """
        _, params = await self.get_parameters()
        post = await self.json_data()
        if not post:
            return self.error(
                request=self.request,
                response="Cannot Insert a row without post data",
                state=406,
            )
        parameters = {**params, **post}
        try:
            qry = self.model(**parameters)
            if qry.is_valid():
                # TODO: if insert fails in constraint, trigger POST (UPDATE)
                result = await self.model.create([parameters])
                if result:
                    data = [row.dict() for row in result]
                return self.model_response(data)
            else:
                return self.error(
                    request=self.request,
                    response=f"Invalid data for Schema {self.Meta.tablename}",
                )
        except Exception as err:
            return self.critical(request=self.request, exception=err, traceback="")

    class Meta:
        tablename: str = ""
