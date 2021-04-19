import signal
import requests
from io import BytesIO
import json
import time
import sys
from contextlib import contextmanager
import os
import shutil
import tempfile
from dataclasses import dataclass
import inspect
import functools
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Any, Type, List, Callable, Dict
from numbers import Number

from flask import Flask, send_file, request, jsonify, abort, Response
from werkzeug.datastructures import FileStorage
import redis

# TODO(andreas): handle directory input
# TODO(andreas): handle List[Dict[str, int]], etc.
# TODO(andreas): model-level documentation

_VALID_INPUT_TYPES = frozenset([str, int, float, bool, Path])
_UNSPECIFIED = object()


class InputValidationError(Exception):
    pass


class Model(ABC):
    @abstractmethod
    def setup(self):
        pass

    @abstractmethod
    def run(self, **kwargs):
        pass


class HTTPServer:
    def __init__(self, model: Model):
        self.model = model

    def make_app(self) -> Flask:
        start_time = time.time()
        self.model.setup()
        app = Flask(__name__)
        setup_time = time.time() - start_time

        @app.route("/infer", methods=["POST"])
        def handle_request():
            start_time = time.time()

            cleanup_functions = []
            try:
                raw_inputs = {}
                for key, val in request.form.items():
                    raw_inputs[key] = val
                for key, val in request.files.items():
                    if key in raw_inputs:
                        return _abort400(
                            f"Duplicated argument name in form and files: {key}"
                        )
                    raw_inputs[key] = val

                if hasattr(self.model.run, "_inputs"):
                    try:
                        inputs = validate_and_convert_inputs(
                            self.model, raw_inputs, cleanup_functions
                        )
                    except InputValidationError as e:
                        return _abort400(str(e))
                else:
                    inputs = raw_inputs

                result = self.model.run(**inputs)
                run_time = time.time() - start_time
                return self.create_response(result, setup_time, run_time)
            finally:
                for cleanup_function in cleanup_functions:
                    try:
                        cleanup_function()
                    except Exception as e:
                        sys.stderr.write(f"Cleanup function caught error: {e}")

        @app.route("/ping")
        def ping():
            return "PONG"

        @app.route("/help")
        def help():
            args = {}
            if hasattr(self.model.run, "_inputs"):
                input_specs = self.model.run._inputs
                for name, spec in input_specs.items():
                    arg = {
                        "type": _type_name(spec.type),
                    }
                    if spec.help:
                        arg["help"] = spec.help
                    if spec.default is not _UNSPECIFIED:
                        arg["default"] = str(spec.default)  # TODO: don't string this
                    if spec.min is not None:
                        arg["min"] = str(spec.min)  # TODO: don't string this
                    if spec.max is not None:
                        arg["max"] = str(spec.max)  # TODO: don't string this
                    args[name] = arg
            return jsonify({"arguments": args})

        return app

    def start_server(self):
        app = self.make_app()
        app.run(host="0.0.0.0", port=5000)

    def create_response(self, result, setup_time, run_time):
        if isinstance(result, Path):
            resp = send_file(str(result))
        elif isinstance(result, str):
            resp = Response(result)
        else:
            resp = jsonify(result)
        resp.headers["X-Setup-Time"] = setup_time
        resp.headers["X-Run-Time"] = run_time
        return resp


class AIPlatformPredictionServer:
    def __init__(self, model: Model):
        sys.stderr.write(
            "WARNING: AIPlatformPredictionServer is experimental, do not use this in production\n"
        )
        self.model = model

    def make_app(self) -> Flask:
        self.model.setup()
        app = Flask(__name__)

        @app.route("/infer", methods=["POST"])
        def handle_request():
            cleanup_functions = []
            try:
                content = request.json
                instances = content["instances"]
                results = []
                for instance in instances:
                    try:
                        validate_and_convert_inputs(
                            self.model, instance, cleanup_functions
                        )
                    except InputValidationError as e:
                        return jsonify({"error": str(e)})
                    results.append(self.model.run(**instance))
                return jsonify(
                    {
                        "predictions": results,
                    }
                )
            except Exception as e:
                tb = traceback.format_exc()
                return jsonify(
                    {
                        "error": tb,
                    }
                )

        @app.route("/ping")
        def ping():
            return "PONG"

        @app.route("/help")
        def help():
            args = {}
            if hasattr(self.model.run, "_inputs"):
                input_specs = self.model.run._inputs
                for name, spec in input_specs.items():
                    arg = {
                        "type": _type_name(spec.type),
                    }
                    if spec.help:
                        arg["help"] = spec.help
                    if spec.default is not _UNSPECIFIED:
                        arg["default"] = str(spec.default)  # TODO: don't string this
                    if spec.min is not None:
                        arg["min"] = str(spec.min)  # TODO: don't string this
                    if spec.max is not None:
                        arg["max"] = str(spec.max)  # TODO: don't string this
                    args[name] = arg
            return jsonify({"arguments": args})

        return app

    def start_server(self):
        app = self.make_app()
        app.run(host="0.0.0.0", port=5000)

    def create_response(self, result, setup_time, run_time):
        if isinstance(result, Path):
            resp = send_file(str(result))
        elif isinstance(result, str):
            resp = Response(result)
        else:
            resp = jsonify(result)
        resp.headers["X-Setup-Time"] = setup_time
        resp.headers["X-Run-Time"] = run_time
        return resp


# TODO: reliable queue
class RedisQueueWorker:
    def __init__(
        self,
        model: Model,
        redis_host: str,
        redis_port: int,
        input_queue: str,
        upload_url: str,
        redis_db: int = 0,
    ):
        self.model = model
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.input_queue = input_queue
        self.upload_url = upload_url
        self.redis_db = redis_db
        self.redis = redis.Redis(
            host=self.redis_host, port=self.redis_port, db=self.redis_db
        )
        self.should_exit = False
        sys.stderr.write(
            f"Connected to Redis: {self.redis_host}:{self.redis_port} (db {self.redis_db})\n"
        )

    def signal_exit(self, signum, frame):
        self.should_exit = True
        sys.stderr.write("Caught SIGTERM, exiting...\n")

    def start(self):
        signal.signal(signal.SIGTERM, self.signal_exit)
        self.model.setup()
        while not self.should_exit:
            try:
                sys.stderr.write(f"Waiting for message on {self.input_queue}\n")
                _, raw_message = self.redis.blpop([self.input_queue])
                message = json.loads(raw_message)
                message_id = message["id"]
                response_queue = message["response_queue"]
                sys.stderr.write(
                    f"Received message {message_id} on {self.input_queue}\n"
                )
                cleanup_functions = []
                try:
                    self.handle_message(
                        message_id, response_queue, message, cleanup_functions
                    )
                except Exception as e:
                    tb = traceback.format_exc()
                    sys.stderr.write(f"Failed to handle message: {tb}\n")
                    self.push_error(response_queue, e)
                finally:
                    for cleanup_function in cleanup_functions:
                        try:
                            cleanup_function()
                        except Exception as e:
                            sys.stderr.write(f"Cleanup function caught error: {e}")
            except Exception as e:
                tb = traceback.format_exc()
                sys.stderr.write(f"Failed to handle message: {tb}\n")

    def handle_message(self, message_id, response_queue, message, cleanup_functions):
        inputs = {}
        raw_inputs = message["inputs"]
        for k, v in raw_inputs.items():
            if "value" in v and v["value"] != "":
                inputs[k] = v["value"]
            else:
                file_url = v["file"]["url"]
                sys.stderr.write(f"Downloading file from {file_url}\n")
                value_bytes = self.download(file_url)
                inputs[k] = FileStorage(
                    stream=BytesIO(value_bytes), filename=v["file"]["name"]
                )
        try:
            inputs = validate_and_convert_inputs(self.model, inputs, cleanup_functions)
        except InputValidationError as e:
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            self.push_error(response_queue, e)
            return

        result = self.model.run(**inputs)
        self.push_result(response_queue, result)

    def download(self, url):
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.content

    def push_error(self, response_queue, error):
        message = json.dumps(
            {
                "error": str(error),
            }
        )
        sys.stderr.write(f"Pushing error to {response_queue}\n")
        self.redis.rpush(response_queue, message)

    def push_result(self, response_queue, result):
        if isinstance(result, Path):
            message = {
                "file": {
                    "url": self.upload_to_temp(result),
                    "name": result.name,
                }
            }
        elif isinstance(result, str):
            message = {
                "value": result,
            }
        else:
            message = {
                "value": json.dumps(result),
            }

        sys.stderr.write(f"Pushing successful result to {response_queue}\n")
        self.redis.rpush(response_queue, json.dumps(message))

    def upload_to_temp(self, path: Path) -> str:
        sys.stderr.write(
            f"Uploading {path.name} to temporary storage at {self.upload_url}\n"
        )
        resp = requests.put(
            self.upload_url, files={"file": (path.name, path.open("rb"))}
        )
        resp.raise_for_status()
        return resp.json()["url"]


def validate_and_convert_inputs(
    model: Model, raw_inputs: Dict[str, Any], cleanup_functions: List[Callable]
) -> Dict[str, Any]:
    input_specs = model.run._inputs
    inputs = {}

    for name, input_spec in input_specs.items():
        if name in raw_inputs:
            val = raw_inputs[name]

            if input_spec.type == Path:
                if not isinstance(val, FileStorage):
                    raise InputValidationError(
                        f"Could not convert file input {name} to {_type_name(input_spec.type)}",
                    )
                if val.filename is None:
                    raise InputValidationError(
                        f"No filename is provided for file input {name}"
                    )

                temp_dir = tempfile.mkdtemp()
                cleanup_functions.append(lambda: shutil.rmtree(temp_dir))

                temp_path = os.path.join(temp_dir, val.filename)
                with open(temp_path, "wb") as f:
                    f.write(val.stream.read())
                converted = Path(temp_path)

            elif input_spec.type == int:
                try:
                    converted = int(val)
                except ValueError:
                    raise InputValidationError(f"Could not convert {name}={val} to int")

            elif input_spec.type == float:
                try:
                    converted = float(val)
                except ValueError:
                    raise InputValidationError(
                        f"Could not convert {name}={val} to float"
                    )

            elif input_spec.type == bool:
                if val not in [True, False]:
                    raise InputValidationError(f"{name}={val} is not a boolean")

            elif input_spec.type == str:
                if isinstance(val, FileStorage):
                    raise InputValidationError(
                        f"Could not convert file input {name} to str"
                    )
                converted = val

            else:
                raise TypeError(
                    f"Internal error: Input type {input_spec} is not a valid input type"
                )

            if _is_numeric_type(input_spec.type):
                if input_spec.max is not None and converted > input_spec.max:
                    raise InputValidationError(
                        f"Value {converted} is greater than the max value {input_spec.max}"
                    )
                if input_spec.min is not None and converted < input_spec.min:
                    raise InputValidationError(
                        f"Value {converted} is less than the min value {input_spec.min}"
                    )

        else:
            if input_spec.default is not _UNSPECIFIED:
                converted = input_spec.default
            else:
                raise InputValidationError(f"Missing expected argument: {name}")
        inputs[name] = converted

    expected_keys = set(input_specs.keys())
    raw_keys = set(raw_inputs.keys())
    extraneous_keys = raw_keys - expected_keys
    if extraneous_keys:
        raise InputValidationError(
            f"Extraneous input keys: {', '.join(extraneous_keys)}"
        )

    return inputs


@contextmanager
def unzip_to_tempdir(zip_path):
    with tempfile.TemporaryDirectory() as tempdir:
        shutil.unpack_archive(zip_path, tempdir, "zip")
        yield tempdir


def make_temp_path(filename):
    temp_dir = make_temp_dir()
    return Path(os.path.join(temp_dir, filename))


def make_temp_dir():
    # TODO(andreas): cleanup
    temp_dir = tempfile.mkdtemp()
    return temp_dir


@dataclass
class InputSpec:
    type: Type
    default: Any = _UNSPECIFIED
    min: Optional[Number] = None
    max: Optional[Number] = None
    help: Optional[str] = None


def input(name, type, default=_UNSPECIFIED, min=None, max=None, help=None):
    type_name = _type_name(type)
    if type not in _VALID_INPUT_TYPES:
        type_list = ", ".join([_type_name(t) for t in _VALID_INPUT_TYPES])
        raise ValueError(
            f"{type_name} is not a valid input type. Valid types are: {type_list}"
        )
    if (min is not None or max is not None) and not _is_numeric_type(type):
        raise ValueError(f"Non-numeric type {type_name} cannot have min and max values")

    def wrapper(f):
        if not hasattr(f, "_inputs"):
            f._inputs = {}

        if name in f._inputs:
            raise ValueError(f"{name} is already defined as an argument")

        if type == Path and default is not _UNSPECIFIED and default is not None:
            raise TypeError("Cannot use default with Path type")

        f._inputs[name] = InputSpec(
            type=type, default=default, min=min, max=max, help=help
        )

        @functools.wraps(f)
        def wraps(self, **kwargs):
            if not isinstance(self, Model):
                raise TypeError("{self} is not an instance of cog.Model")
            return f(self, **kwargs)

        return wraps

    return wrapper


def _type_name(typ: Type) -> str:
    if typ == str:
        return "str"
    if typ == int:
        return "int"
    if typ == float:
        return "float"
    if typ == bool:
        return "bool"
    if typ == Path:
        return "Path"
    return str(typ)


def _is_numeric_type(typ: Type) -> bool:
    return typ in (int, float)


def _method_arg_names(f) -> List[str]:
    return inspect.getfullargspec(f)[0][1:]  # 0 is self


def _abort400(message):
    resp = jsonify({"message": message})
    resp.status_code = 400
    return resp
