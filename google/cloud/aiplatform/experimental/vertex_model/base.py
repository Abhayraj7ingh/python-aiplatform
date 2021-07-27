# -*- coding: utf-8 -*-

# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import abc
import datetime
import functools
import inspect
import pathlib
import tempfile
from typing import Any
from typing import Callable


from google.cloud import aiplatform
from google.cloud.aiplatform import base
from google.cloud.aiplatform.experimental.vertex_model.serializers import pandas
from google.cloud.aiplatform.experimental.vertex_model.utils import source_utils

try:
    import pandas as pd
except ImportError:
    raise ImportError(
        "Pandas is not installed. Please install pandas to use VertexModel"
    )


_LOGGER = base.Logger(__name__)


def vertex_fit_function_wrapper(method: Callable[..., Any]):
    """Adapts code in the user-written child class for cloud training and prediction

    If the user wishes to conduct local development, will return the original function.
    If not, converts the child class to an executable inner script and calls the Vertex
    AI SDK using the custom training job interface.

    Args:
        method (classmethod): the method to be wrapped.

    Returns:
        A function that will complete local or cloud training based off of the user's
        implementation of the VertexModel class. The training mode is determined by the
        user-designated training_mode variable.

    Raises:
        RuntimeError: An error occurred trying to access the staging bucket.
    """

    @functools.wraps(method)
    def f(*args, **kwargs):
        if method.__self__.training_mode == "local":
            return method(*args, **kwargs)

        obj = method.__self__
        cls_name = obj.__class__.__name__

        training_source = source_utils._make_class_source(obj)

        bound_args = inspect.signature(method).bind(*args, **kwargs)

        # get the mapping of parameter names to types
        # split the arguments into those that we need to serialize and those that can
        # be hard coded into the source

        pass_through_params = {}
        serialized_params = {}

        for parameter_name, parameter in bound_args.arguments.items():
            parameter_type = type(parameter)
            valid_types = [int, float, str] + list(
                obj._data_serialization_mapping.keys()
            )
            if parameter_type not in valid_types:
                raise RuntimeError(
                    f"{parameter_type} not supported. parameter_name = {parameter_name}. The only supported types are {valid_types}"
                )

            if type(parameter) in obj._data_serialization_mapping.keys():
                serialized_params[parameter_name] = parameter
            else:  # assume primitive
                pass_through_params[parameter_name] = parameter

        staging_bucket = aiplatform.initializer.global_config.staging_bucket
        if staging_bucket is None:
            raise RuntimeError(
                "Staging bucket must be set to run training in cloud mode: `aiplatform.init(staging_bucket='gs://my/staging/bucket')`"
            )

        timestamp = datetime.datetime.now().isoformat(sep="-", timespec="milliseconds")
        vertex_model_root_folder = "/".join(
            [staging_bucket, f"vertex_model_run_{timestamp}"]
        )

        param_name_to_serialized_info = {}
        serialized_inputs_artifacts_folder = "/".join(
            [vertex_model_root_folder, "serialized_input_parameters"]
        )
        print(serialized_inputs_artifacts_folder)

        for parameter_name, parameter in serialized_params.items():
            serializer = obj._data_serialization_mapping[type(parameter)][1]
            parameter_uri = serializer(
                serialized_inputs_artifacts_folder, parameter, parameter_name
            )

            # namedtuple
            param_name_to_serialized_info[parameter_name] = (
                parameter_uri,
                type(parameter),
            )  # "pd.DataFrame"

            _LOGGER.info(
                f"{parameter_name} of type {parameter_type} was serialized to {parameter_uri}"
            )

        with tempfile.TemporaryDirectory() as tmpdirname:
            script_path = pathlib.Path(tmpdirname) / "training_script.py"

            source = source_utils._make_source(
                cls_source=training_source,
                cls_name=cls_name,
                instance_method=method.__name__,
                pass_through_params=pass_through_params,
                param_name_to_serialized_info=param_name_to_serialized_info,
                obj=obj,
            )

            with open(script_path, "w") as f:
                f.write(source)

                obj._training_job = aiplatform.CustomTrainingJob(
                    display_name="my_training_job",
                    script_path=str(script_path),
                    # programatically determine the dependency in the future
                    requirements=["pandas>=1.3"],
                    # https://cloud.google.com/vertex-ai/docs/training/pre-built-containers
                    container_uri="us-docker.pkg.dev/vertex-ai/training/pytorch-xla.1-7:latest",
                )

                # In the custom training job, a MODEL directory will be provided as an env var
                # our code should serialize our MODEL to that directory

                obj._training_job.run(replica_count=1)

    return f


class VertexModel(metaclass=abc.ABCMeta):

    _data_serialization_mapping = {
        pd.DataFrame: (pandas._deserialize_dataframe, pandas._serialize_dataframe)
    }

    """ Parent class that users can extend to use the Vertex AI SDK """

    def __init__(self):
        # Default to local training on creation, at least for this prototype.
        self.training_mode = "local"
        self.fit = vertex_fit_function_wrapper(self.fit)

    @abc.abstractmethod
    def fit(self):
        """Train model."""
        pass

    def predict(self):
        """Make predictions on training data."""
        raise NotImplementedError("predict is currently not implemented.")

    def batch_predict(self):
        """Make predictions on training data."""
        raise NotImplementedError("batch_predict is currently not implemented.")

    def eval(self):
        """Evaluate model."""
        raise NotImplementedError("eval is currently not implemented.")
