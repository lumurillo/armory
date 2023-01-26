import copy
from importlib import import_module

from PIL import Image
import numpy as np
import torch
import tensorflow as tf

from armory.data.utils import maybe_download_weights_from_s3

# An armory user may request one of these models under 'adhoc'/'explanatory_model'
EXPLANATORY_MODEL_CONFIGS = explanatory_model_configs = {
    "speech_commands_explanatory_model": {
        "module": "armory.baseline_models.tf_graph.audio_resnet50",
        "name": "get_unwrapped_model",
        "data_modality": "audio",
        "activation_layer": "avg_pool",
        "model_framework": "tensorflow",
        "weights_file": "speech_commands_explanatory_model_resnet50_bean.h5",
    },
    "cifar10_explanatory_model": {
        "model_kwargs": {
            "data_means": [0.4914, 0.4822, 0.4465],
            "data_stds": [0.2471, 0.2435, 0.2616],
            "num_classes": 10,
        },
        "module": "armory.baseline_models.pytorch.resnet18_bean_regularization",
        "name": "get_model",
        "resize_image": False,
        "weights_file": "cifar10_explanatory_model_resnet18_bean.pt",
    },
    "gtsrb_explanatory_model": {
        "model_kwargs": {},
        "module": "armory.baseline_models.pytorch.micronnet_gtsrb_bean_regularization",
        "name": "get_model",
        "resize_image": False,
        "weights_file": "gtsrb_explanatory_model_micronnet_bean.pt",
    },
    "resisc10_explanatory_model": {
        "model_kwargs": {
            "data_means": [0.39382024, 0.4159701, 0.40887499],
            "data_stds": [0.18931773, 0.18901625, 0.19651154],
            "num_classes": 10,
        },
        "module": "armory.baseline_models.pytorch.resnet18_bean_regularization",
        "name": "get_model",
        "weights_file": "resisc10_explanatory_model_resnet18_bean.pt",
    },
}

DEVICE = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")


class ExplanatoryModel:
    def __init__(
        self,
        explanatory_model,
        data_modality="image",
        model_framework="pytorch",
        activation_layer=None,
        resize_image=True,
        size=(224, 224),
        resample=Image.BILINEAR,
        device=DEVICE,
    ):
        if not callable(explanatory_model):
            raise ValueError(f"explanatory_model {explanatory_model} is not callable")
        self.explanatory_model = explanatory_model
        self.data_modality = data_modality
        self.model_framework = model_framework
        self.activation_layer = activation_layer
        self.resize_image = bool(resize_image)
        self.size = size
        self.resample = resample
        self.device = device

        if self.model_framework == "tensorflow" and self.activation_layer is not None:
            self.explanatory_model = tf.keras.Model(
                explanatory_model.layers[0].input,
                explanatory_model.get_layer(self.activation_layer).output,
            )

    @classmethod
    def from_config(cls, model_config, **kwargs):
        if isinstance(model_config, str):
            if model_config not in EXPLANATORY_MODEL_CONFIGS:
                raise ValueError(
                    f"model_config {model_config}, if a str, must be in {EXPLANATORY_MODEL_CONFIGS.keys()}"
                )
            model_config = EXPLANATORY_MODEL_CONFIGS[model_config]
        if not isinstance(model_config, dict):
            raise ValueError(
                f"model_config {model_config} must be a str or dict, not {type(model_config)}"
            )
        model_config = copy.copy(model_config)
        model_config.update(kwargs)  # override config with kwargs
        keys = ("module", "name", "weights_file")
        for k in keys:
            if k not in model_config:
                raise ValueError(f"config key {k} is required")
        module, name, weights_file = (model_config.pop(k) for k in keys)
        model_kwargs = model_config.pop("model_kwargs", {})
        data_modality = model_config.pop("data_modality", "image")
        model_framework = model_config.pop("model_framework", "pytorch")
        activation_layer = model_config.pop("activation_layer", None)

        weights_path = maybe_download_weights_from_s3(
            weights_file, auto_expand_tars=True
        )
        model_module = import_module(module)
        model_fn = getattr(model_module, name)
        explanatory_model = model_fn(weights_path, **model_kwargs)

        return cls(
            explanatory_model,
            data_modality,
            model_framework,
            activation_layer,
            **model_config,
        )

    def get_activations(self, x, batch_size: int = None):
        """
        Return array of activations from input batch x

        if batch_size, batch inputs and then concatenate
        """
        activations = []
        if batch_size:
            batch_size = int(batch_size)
            if batch_size < 1:
                raise ValueError("batch_size must be false or a positive int")
        else:
            batch_size = len(x)

        for i in range(0, len(x), batch_size):
            x_batch = x[i : i + batch_size]

            if self.model_framework == "pytorch":
                with torch.no_grad():
                    x_batch = self.preprocess(x_batch)
                    activation, _ = self.explanatory_model(x_batch)
                    activations.append(activation.detach().cpu().numpy())

            elif self.model_framework == "tensorflow":
                x_batch = self.preprocess(x_batch)
                activation = self.explanatory_model(x_batch, training=False)
                activations.append(activation.numpy())

        return np.concatenate(activations)

    @staticmethod
    def _preprocess_image(
        x, resize_image=True, size=(224, 224), resample=Image.BILINEAR, device=DEVICE
    ):
        if np.issubdtype(x.dtype, np.floating):
            if x.min() < 0.0 or x.max() > 1.0:
                raise ValueError("Floating input not bound to [0.0, 1.0] range")

            if resize_image:
                x = np.round(x * 255).astype(np.uint8)
            elif x.dtype != np.float32:
                x = x.astype(np.float32)
        elif x.dtype == np.uint8:
            if not resize_image:
                x = x.astype(np.float32) / 255
        else:
            raise ValueError(
                f"Input must be of type np.uint8 or floating, not {x.dtype}"
            )
        if resize_image:
            images = []
            for i in range(len(x)):
                image = Image.fromarray(x[i])
                image = image.resize(size=size, resample=resample)
                images.append(np.array(image, dtype=np.float32))
            x = np.stack(images) / 255

        return torch.tensor(x).to(device)

    def preprocess(self, x):
        """
        Preprocess a batch of images
        """
        if self.data_modality == "image":
            return type(self)._preprocess_image(
                x,
                self.resize_image,
                self.size,
                resample=self.resample,
                device=self.device,
            )
        elif self.data_modality == "audio":
            return x
