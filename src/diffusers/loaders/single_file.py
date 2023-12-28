# Copyright 2023 The HuggingFace Team. All rights reserved.
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
import importlib
import inspect
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path

import requests
import torch
from huggingface_hub import hf_hub_download
from huggingface_hub.utils import validate_hf_hub_args
from safetensors.torch import load_file as safe_load

from ..utils import (
    deprecate,
    is_accelerate_available,
    is_omegaconf_available,
    is_transformers_available,
    logging,
)
from ..utils.import_utils import BACKENDS_MAPPING
from .single_file_utils import (
    create_scheduler_components,
    create_stable_unclip_components,
    create_unet_model,
    create_vae_model,
    download_from_original_stable_diffusion_ckpt,
    fetch_original_config,
)


if is_transformers_available():
    pass

if is_accelerate_available():
    from accelerate import init_empty_weights

logger = logging.get_logger(__name__)


VALID_URL_PREFIXES = ["https://huggingface.co/", "huggingface.co/", "hf.co/", "https://hf.co/"]
TEXT_ENCODER_FROM_PIPELINE_CLASS = {
    "StableUnCLIPPipeline": "FrozenOpenCLIPEmbedder",
    "StableUnCLIPImg2ImgPipeline": "FrozenOpenCLIPEmbedder",
    "LDMTextToImagePipeline": "LDMTextToImage",
    "PaintByExamplePipeline": "PaintByExample",
    "StableDiffusion": "stable-diffusion",
}


def extract_pipeline_component_names(pipeline_class):
    components = inspect.signature(pipeline_class).parameters.keys()
    return components


def check_valid_url(pretrained_model_link_or_path):
    # remove huggingface url
    has_valid_url_prefix = False
    for prefix in VALID_URL_PREFIXES:
        if pretrained_model_link_or_path.startswith(prefix):
            pretrained_model_link_or_path = pretrained_model_link_or_path[len(prefix) :]
            has_valid_url_prefix = True

    return has_valid_url_prefix


def download_model_checkpoint(
    ckpt_path,
    cache_dir=None,
    resume_download=False,
    force_download=False,
    proxies=None,
    local_files_only=None,
    token=None,
    revision=None,
):
    # get repo_id and (potentially nested) file path of ckpt in repo
    repo_id = "/".join(ckpt_path.parts[:2])
    file_path = "/".join(ckpt_path.parts[2:])

    if file_path.startswith("blob/"):
        file_path = file_path[len("blob/") :]

    if file_path.startswith("main/"):
        file_path = file_path[len("main/") :]

    path = hf_hub_download(
        repo_id,
        filename=file_path,
        cache_dir=cache_dir,
        resume_download=resume_download,
        proxies=proxies,
        local_files_only=local_files_only,
        token=token,
        revision=revision,
        force_download=force_download,
    )

    return path


def load_checkpoint(checkpoint_path_or_dict, device=None, from_safetensors=True):
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    if isinstance(checkpoint_path_or_dict, str):
        if from_safetensors:
            checkpoint = safe_load(checkpoint_path_or_dict, device="cpu")

        else:
            checkpoint = torch.load(checkpoint_path_or_dict, map_location=device)

    elif isinstance(checkpoint_path_or_dict, dict):
        checkpoint = checkpoint_path_or_dict

    return checkpoint


def build_component(
    pipeline_components,
    pipeline_class_name,
    component_name,
    original_config,
    checkpoint,
    checkpoint_path_or_dict,
    **kwargs,
):
    if component_name in kwargs:
        return kwargs.pop(component_name, None)

    if component_name in pipeline_components:
        return {}

    if component_name == "unet":
        unet_components = create_unet_model(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return unet_components

    if component_name == "vae":
        vae_components = create_vae_model(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return vae_components

    if component_name == "controlnet":
        controlnet_components = create_controlnet_model(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return controlnet_components

    if component_name == "adapter":
        adapter_components = create_adapter_model(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return adapter_components

    if component_name == "scheduler":
        scheduler_components = create_scheduler(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return scheduler_components

    if component_name in ["text_encoder", "text_encoder_2", "tokenizer", "tokenizer_2"]:
        text_encoder_components = create_text_encoders_and_tokenizers(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return text_encoder_components

    return


def build_additional_components(
    pipeline_components,
    pipeline_class_name,
    component_name,
    original_config,
    checkpoint,
    checkpoint_path_or_dict,
    **kwargs,
):
    if component_name in kwargs:
        return kwargs.pop(component_name, None)

    if component_name in pipeline_components:
        return {}

    local_files_only = kwargs.pop("local_files_only", False)

    if pipeline_class_name == ["StableUnCLIPPipeline", "StableUnCLIPImg2ImgPipeline"]:
        stable_unclip_components = create_stable_unclip_components(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return stable_unclip_components

    if pipeline_class_name == "LDMTextToImagePipeline":
        ldm_text_to_image_components = create_ldm_text_to_image_components(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return ldm_text_to_image_components

    if pipeline_class_name == "PaintByExamplePipeline":
        paint_by_example_components = create_paint_by_example_components(
            pipeline_class_name, original_config, checkpoint, checkpoint_path_or_dict, **kwargs
        )
        return paint_by_example_components


class FromSingleFileMixin:
    """
    Load model weights saved in the `.ckpt` format into a [`DiffusionPipeline`].
    """

    @classmethod
    def from_ckpt(cls, *args, **kwargs):
        deprecation_message = "The function `from_ckpt` is deprecated in favor of `from_single_file` and will be removed in diffusers v.0.21. Please make sure to use `StableDiffusionPipeline.from_single_file(...)` instead."
        deprecate("from_ckpt", "0.21.0", deprecation_message, standard_warn=False)
        return cls.from_single_file(*args, **kwargs)

    @classmethod
    @validate_hf_hub_args
    def from_single_file(cls, pretrained_model_link_or_path, **kwargs):
        r"""
        Instantiate a [`DiffusionPipeline`] from pretrained pipeline weights saved in the `.ckpt` or `.safetensors`
        format. The pipeline is set in evaluation mode (`model.eval()`) by default.

        Parameters:
            pretrained_model_link_or_path (`str` or `os.PathLike`, *optional*):
                Can be either:
                    - A link to the `.ckpt` file (for example
                      `"https://huggingface.co/<repo_id>/blob/main/<path_to_file>.ckpt"`) on the Hub.
                    - A path to a *file* containing all pipeline weights.
            torch_dtype (`str` or `torch.dtype`, *optional*):
                Override the default `torch.dtype` and load the model with another dtype. If `"auto"` is passed, the
                dtype is automatically derived from the model's weights.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.
            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            resume_download (`bool`, *optional*, defaults to `False`):
                Whether or not to resume downloading the model weights and configuration files. If set to `False`, any
                incompletely downloaded files are deleted.
            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to `True`, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            use_safetensors (`bool`, *optional*, defaults to `None`):
                If set to `None`, the safetensors weights are downloaded if they're available **and** if the
                safetensors library is installed. If set to `True`, the model is forcibly loaded from safetensors
                weights. If set to `False`, safetensors weights are not loaded.
            extract_ema (`bool`, *optional*, defaults to `False`):
                Whether to extract the EMA weights or not. Pass `True` to extract the EMA weights which usually yield
                higher quality images for inference. Non-EMA weights are usually better for continuing finetuning.
            upcast_attention (`bool`, *optional*, defaults to `None`):
                Whether the attention computation should always be upcasted.
            image_size (`int`, *optional*, defaults to 512):
                The image size the model was trained on. Use 512 for all Stable Diffusion v1 models and the Stable
                Diffusion v2 base model. Use 768 for Stable Diffusion v2.
            prediction_type (`str`, *optional*):
                The prediction type the model was trained on. Use `'epsilon'` for all Stable Diffusion v1 models and
                the Stable Diffusion v2 base model. Use `'v_prediction'` for Stable Diffusion v2.
            num_in_channels (`int`, *optional*, defaults to `None`):
                The number of input channels. If `None`, it is automatically inferred.
            scheduler_type (`str`, *optional*, defaults to `"pndm"`):
                Type of scheduler to use. Should be one of `["pndm", "lms", "heun", "euler", "euler-ancestral", "dpm",
                "ddim"]`.
            load_safety_checker (`bool`, *optional*, defaults to `True`):
                Whether to load the safety checker or not.
            text_encoder ([`~transformers.CLIPTextModel`], *optional*, defaults to `None`):
                An instance of `CLIPTextModel` to use, specifically the
                [clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14) variant. If this
                parameter is `None`, the function loads a new instance of `CLIPTextModel` by itself if needed.
            vae (`AutoencoderKL`, *optional*, defaults to `None`):
                Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations. If
                this parameter is `None`, the function will load a new instance of [CLIP] by itself, if needed.
            tokenizer ([`~transformers.CLIPTokenizer`], *optional*, defaults to `None`):
                An instance of `CLIPTokenizer` to use. If this parameter is `None`, the function loads a new instance
                of `CLIPTokenizer` by itself if needed.
            original_config_file (`str`):
                Path to `.yaml` config file corresponding to the original architecture. If `None`, will be
                automatically inferred by looking for a key that only exists in SD2.0 models.
            kwargs (remaining dictionary of keyword arguments, *optional*):
                Can be used to overwrite load and saveable variables (for example the pipeline components of the
                specific pipeline class). The overwritten components are directly passed to the pipelines `__init__`
                method. See example below for more information.

        Examples:

        ```py
        >>> from diffusers import StableDiffusionPipeline

        >>> # Download pipeline from huggingface.co and cache.
        >>> pipeline = StableDiffusionPipeline.from_single_file(
        ...     "https://huggingface.co/WarriorMama777/OrangeMixs/blob/main/Models/AbyssOrangeMix/AbyssOrangeMix.safetensors"
        ... )

        >>> # Download pipeline from local file
        >>> # file is downloaded under ./v1-5-pruned-emaonly.ckpt
        >>> pipeline = StableDiffusionPipeline.from_single_file("./v1-5-pruned-emaonly")

        >>> # Enable float16 and move to GPU
        >>> pipeline = StableDiffusionPipeline.from_single_file(
        ...     "https://huggingface.co/runwayml/stable-diffusion-v1-5/blob/main/v1-5-pruned-emaonly.ckpt",
        ...     torch_dtype=torch.float16,
        ... )
        >>> pipeline.to("cuda")
        ```
        """
        original_config_file = kwargs.pop("original_config_file", None)
        config_files = kwargs.pop("config_files", None)
        resume_download = kwargs.pop("resume_download", False)
        proxies = kwargs.pop("proxies", None)
        token = kwargs.pop("token", None)
        cache_dir = kwargs.pop("cache_dir", None)
        local_files_only = kwargs.pop("local_files_only", None)
        revision = kwargs.pop("revision", None)
        torch_dtype = kwargs.pop("torch_dtype", None)
        use_safetensors = kwargs.pop("use_safetensors", None)
        load_safety_checker = kwargs.pop("load_safety_checker", True)

        extract_ema = kwargs.pop("extract_ema", False)
        image_size = kwargs.pop("image_size", None)
        scheduler_type = kwargs.pop("scheduler_type", "pndm")
        num_in_channels = kwargs.pop("num_in_channels", None)
        upcast_attention = kwargs.pop("upcast_attention", None)
        prediction_type = kwargs.pop("prediction_type", None)

        pipeline_name = cls.__name__
        file_extension = pretrained_model_link_or_path.rsplit(".", 1)[-1]
        from_safetensors = file_extension == "safetensors"

        if from_safetensors and use_safetensors is False:
            raise ValueError("Make sure to install `safetensors` with `pip install safetensors`.")

        has_valid_url_prefix = check_valid_url(pretrained_model_link_or_path)

        # Code based on diffusers.pipelines.pipeline_utils.DiffusionPipeline.from_pretrained
        ckpt_path = Path(pretrained_model_link_or_path)
        if (not ckpt_path.is_file()) and (not has_valid_url_prefix):
            raise ValueError(
                f"The provided path is either not a file or a valid huggingface URL was not provided. Valid URLs begin with {', '.join(VALID_URL_PREFIXES)}"
            )
        pretrained_model_link_or_path = download_model_checkpoint(
            ckpt_path,
            cache_dir=cache_dir,
            resume_download=resume_download,
            proxies=proxies,
            local_files_only=local_files_only,
            token=token,
            revision=revision,
        )
        checkpoint = load_checkpoint(pretrained_model_link_or_path, from_safetensors=from_safetensors)

        # NOTE: this while loop isn't great but this controlnet checkpoint has one additional
        # "state_dict" key https://huggingface.co/thibaud/controlnet-canny-sd21
        while "state_dict" in checkpoint:
            checkpoint = checkpoint["state_dict"]

        original_config = fetch_original_config(checkpoint, config_files)
        component_names = extract_pipeline_component_names(cls)

        pipeline_components = {}
        for component in component_names:
            components = build_component(
                pipeline_components, pipeline_name, component, checkpoint, original_config, **kwargs
            )
            pipeline_components.update(components)

        additional_components = set(pipeline_components.keys() - component_names)
        if additional_components:
            components = build_additional_components(pipeline_name, component, checkpoint, original_config, **kwargs)
            pipeline_components.update(components)

        pipe = cls(**pipeline_components)

        if torch_dtype is not None:
            pipe.to(dtype=torch_dtype)

        return pipe


class FromOriginalVAEMixin:
    """
    Load pretrained ControlNet weights saved in the `.ckpt` or `.safetensors` format into an [`AutoencoderKL`].
    """

    @classmethod
    @validate_hf_hub_args
    def from_single_file(cls, pretrained_model_link_or_path, **kwargs):
        r"""
        Instantiate a [`AutoencoderKL`] from pretrained ControlNet weights saved in the original `.ckpt` or
        `.safetensors` format. The pipeline is set in evaluation mode (`model.eval()`) by default.

        Parameters:
            pretrained_model_link_or_path (`str` or `os.PathLike`, *optional*):
                Can be either:
                    - A link to the `.ckpt` file (for example
                      `"https://huggingface.co/<repo_id>/blob/main/<path_to_file>.ckpt"`) on the Hub.
                    - A path to a *file* containing all pipeline weights.
            torch_dtype (`str` or `torch.dtype`, *optional*):
                Override the default `torch.dtype` and load the model with another dtype. If `"auto"` is passed, the
                dtype is automatically derived from the model's weights.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.
            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            resume_download (`bool`, *optional*, defaults to `False`):
                Whether or not to resume downloading the model weights and configuration files. If set to `False`, any
                incompletely downloaded files are deleted.
            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to True, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            image_size (`int`, *optional*, defaults to 512):
                The image size the model was trained on. Use 512 for all Stable Diffusion v1 models and the Stable
                Diffusion v2 base model. Use 768 for Stable Diffusion v2.
            use_safetensors (`bool`, *optional*, defaults to `None`):
                If set to `None`, the safetensors weights are downloaded if they're available **and** if the
                safetensors library is installed. If set to `True`, the model is forcibly loaded from safetensors
                weights. If set to `False`, safetensors weights are not loaded.
            upcast_attention (`bool`, *optional*, defaults to `None`):
                Whether the attention computation should always be upcasted.
            scaling_factor (`float`, *optional*, defaults to 0.18215):
                The component-wise standard deviation of the trained latent space computed using the first batch of the
                training set. This is used to scale the latent space to have unit variance when training the diffusion
                model. The latents are scaled with the formula `z = z * scaling_factor` before being passed to the
                diffusion model. When decoding, the latents are scaled back to the original scale with the formula: `z
                = 1 / scaling_factor * z`. For more details, refer to sections 4.3.2 and D.1 of the [High-Resolution
                Image Synthesis with Latent Diffusion Models](https://arxiv.org/abs/2112.10752) paper.
            kwargs (remaining dictionary of keyword arguments, *optional*):
                Can be used to overwrite load and saveable variables (for example the pipeline components of the
                specific pipeline class). The overwritten components are directly passed to the pipelines `__init__`
                method. See example below for more information.

        <Tip warning={true}>

            Make sure to pass both `image_size` and `scaling_factor` to `from_single_file()` if you're loading
            a VAE from SDXL or a Stable Diffusion v2 model or higher.

        </Tip>

        Examples:

        ```py
        from diffusers import AutoencoderKL

        url = "https://huggingface.co/stabilityai/sd-vae-ft-mse-original/blob/main/vae-ft-mse-840000-ema-pruned.safetensors"  # can also be local file
        model = AutoencoderKL.from_single_file(url)
        ```
        """
        if not is_omegaconf_available():
            raise ValueError(BACKENDS_MAPPING["omegaconf"][1])

        from omegaconf import OmegaConf

        from ..models import AutoencoderKL

        # import here to avoid circular dependency
        from ..pipelines.stable_diffusion.convert_from_ckpt import (
            convert_ldm_vae_checkpoint,
            create_vae_diffusers_config,
        )

        config_file = kwargs.pop("config_file", None)
        cache_dir = kwargs.pop("cache_dir", None)
        resume_download = kwargs.pop("resume_download", False)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        revision = kwargs.pop("revision", None)
        image_size = kwargs.pop("image_size", None)
        scaling_factor = kwargs.pop("scaling_factor", None)
        kwargs.pop("upcast_attention", None)

        torch_dtype = kwargs.pop("torch_dtype", None)

        use_safetensors = kwargs.pop("use_safetensors", None)

        file_extension = pretrained_model_link_or_path.rsplit(".", 1)[-1]
        from_safetensors = file_extension == "safetensors"

        if from_safetensors and use_safetensors is False:
            raise ValueError("Make sure to install `safetensors` with `pip install safetensors`.")

        # remove huggingface url
        for prefix in ["https://huggingface.co/", "huggingface.co/", "hf.co/", "https://hf.co/"]:
            if pretrained_model_link_or_path.startswith(prefix):
                pretrained_model_link_or_path = pretrained_model_link_or_path[len(prefix) :]

        # Code based on diffusers.pipelines.pipeline_utils.DiffusionPipeline.from_pretrained
        ckpt_path = Path(pretrained_model_link_or_path)
        if not ckpt_path.is_file():
            # get repo_id and (potentially nested) file path of ckpt in repo
            repo_id = "/".join(ckpt_path.parts[:2])
            file_path = "/".join(ckpt_path.parts[2:])

            if file_path.startswith("blob/"):
                file_path = file_path[len("blob/") :]

            if file_path.startswith("main/"):
                file_path = file_path[len("main/") :]

            pretrained_model_link_or_path = hf_hub_download(
                repo_id,
                filename=file_path,
                cache_dir=cache_dir,
                resume_download=resume_download,
                proxies=proxies,
                local_files_only=local_files_only,
                token=token,
                revision=revision,
                force_download=force_download,
            )

        if from_safetensors:
            from safetensors import safe_open

            checkpoint = {}
            with safe_open(pretrained_model_link_or_path, framework="pt", device="cpu") as f:
                for key in f.keys():
                    checkpoint[key] = f.get_tensor(key)
        else:
            checkpoint = torch.load(pretrained_model_link_or_path, map_location="cpu")

        if "state_dict" in checkpoint:
            checkpoint = checkpoint["state_dict"]

        if config_file is None:
            config_url = "https://raw.githubusercontent.com/CompVis/stable-diffusion/main/configs/stable-diffusion/v1-inference.yaml"
            config_file = BytesIO(requests.get(config_url).content)

        original_config = OmegaConf.load(config_file)

        # default to sd-v1-5
        image_size = image_size or 512

        vae_config = create_vae_diffusers_config(original_config, image_size=image_size)
        converted_vae_checkpoint = convert_ldm_vae_checkpoint(checkpoint, vae_config)

        if scaling_factor is None:
            if (
                "model" in original_config
                and "params" in original_config.model
                and "scale_factor" in original_config.model.params
            ):
                vae_scaling_factor = original_config.model.params.scale_factor
            else:
                vae_scaling_factor = 0.18215  # default SD scaling factor

        vae_config["scaling_factor"] = vae_scaling_factor

        ctx = init_empty_weights if is_accelerate_available() else nullcontext
        with ctx():
            vae = AutoencoderKL(**vae_config)

        if is_accelerate_available():
            from ..models.modeling_utils import load_model_dict_into_meta

            load_model_dict_into_meta(vae, converted_vae_checkpoint, device="cpu")
        else:
            vae.load_state_dict(converted_vae_checkpoint)

        if torch_dtype is not None:
            vae.to(dtype=torch_dtype)

        return vae


class FromOriginalControlnetMixin:
    """
    Load pretrained ControlNet weights saved in the `.ckpt` or `.safetensors` format into a [`ControlNetModel`].
    """

    @classmethod
    @validate_hf_hub_args
    def from_single_file(cls, pretrained_model_link_or_path, **kwargs):
        r"""
        Instantiate a [`ControlNetModel`] from pretrained ControlNet weights saved in the original `.ckpt` or
        `.safetensors` format. The pipeline is set in evaluation mode (`model.eval()`) by default.

        Parameters:
            pretrained_model_link_or_path (`str` or `os.PathLike`, *optional*):
                Can be either:
                    - A link to the `.ckpt` file (for example
                      `"https://huggingface.co/<repo_id>/blob/main/<path_to_file>.ckpt"`) on the Hub.
                    - A path to a *file* containing all pipeline weights.
            torch_dtype (`str` or `torch.dtype`, *optional*):
                Override the default `torch.dtype` and load the model with another dtype. If `"auto"` is passed, the
                dtype is automatically derived from the model's weights.
            force_download (`bool`, *optional*, defaults to `False`):
                Whether or not to force the (re-)download of the model weights and configuration files, overriding the
                cached versions if they exist.
            cache_dir (`Union[str, os.PathLike]`, *optional*):
                Path to a directory where a downloaded pretrained model configuration is cached if the standard cache
                is not used.
            resume_download (`bool`, *optional*, defaults to `False`):
                Whether or not to resume downloading the model weights and configuration files. If set to `False`, any
                incompletely downloaded files are deleted.
            proxies (`Dict[str, str]`, *optional*):
                A dictionary of proxy servers to use by protocol or endpoint, for example, `{'http': 'foo.bar:3128',
                'http://hostname': 'foo.bar:4012'}`. The proxies are used on each request.
            local_files_only (`bool`, *optional*, defaults to `False`):
                Whether to only load local model weights and configuration files or not. If set to True, the model
                won't be downloaded from the Hub.
            token (`str` or *bool*, *optional*):
                The token to use as HTTP bearer authorization for remote files. If `True`, the token generated from
                `diffusers-cli login` (stored in `~/.huggingface`) is used.
            revision (`str`, *optional*, defaults to `"main"`):
                The specific model version to use. It can be a branch name, a tag name, a commit id, or any identifier
                allowed by Git.
            use_safetensors (`bool`, *optional*, defaults to `None`):
                If set to `None`, the safetensors weights are downloaded if they're available **and** if the
                safetensors library is installed. If set to `True`, the model is forcibly loaded from safetensors
                weights. If set to `False`, safetensors weights are not loaded.
            image_size (`int`, *optional*, defaults to 512):
                The image size the model was trained on. Use 512 for all Stable Diffusion v1 models and the Stable
                Diffusion v2 base model. Use 768 for Stable Diffusion v2.
            upcast_attention (`bool`, *optional*, defaults to `None`):
                Whether the attention computation should always be upcasted.
            kwargs (remaining dictionary of keyword arguments, *optional*):
                Can be used to overwrite load and saveable variables (for example the pipeline components of the
                specific pipeline class). The overwritten components are directly passed to the pipelines `__init__`
                method. See example below for more information.

        Examples:

        ```py
        from diffusers import StableDiffusionControlNetPipeline, ControlNetModel

        url = "https://huggingface.co/lllyasviel/ControlNet-v1-1/blob/main/control_v11p_sd15_canny.pth"  # can also be a local path
        model = ControlNetModel.from_single_file(url)

        url = "https://huggingface.co/runwayml/stable-diffusion-v1-5/blob/main/v1-5-pruned.safetensors"  # can also be a local path
        pipe = StableDiffusionControlNetPipeline.from_single_file(url, controlnet=controlnet)
        ```
        """
        # import here to avoid circular dependency
        from ..pipelines.stable_diffusion.convert_from_ckpt import download_controlnet_from_original_ckpt

        config_file = kwargs.pop("config_file", None)
        cache_dir = kwargs.pop("cache_dir", None)
        resume_download = kwargs.pop("resume_download", False)
        force_download = kwargs.pop("force_download", False)
        proxies = kwargs.pop("proxies", None)
        local_files_only = kwargs.pop("local_files_only", None)
        token = kwargs.pop("token", None)
        num_in_channels = kwargs.pop("num_in_channels", None)
        use_linear_projection = kwargs.pop("use_linear_projection", None)
        revision = kwargs.pop("revision", None)
        extract_ema = kwargs.pop("extract_ema", False)
        image_size = kwargs.pop("image_size", None)
        upcast_attention = kwargs.pop("upcast_attention", None)

        torch_dtype = kwargs.pop("torch_dtype", None)

        use_safetensors = kwargs.pop("use_safetensors", None)

        file_extension = pretrained_model_link_or_path.rsplit(".", 1)[-1]
        from_safetensors = file_extension == "safetensors"

        if from_safetensors and use_safetensors is False:
            raise ValueError("Make sure to install `safetensors` with `pip install safetensors`.")

        # remove huggingface url
        for prefix in ["https://huggingface.co/", "huggingface.co/", "hf.co/", "https://hf.co/"]:
            if pretrained_model_link_or_path.startswith(prefix):
                pretrained_model_link_or_path = pretrained_model_link_or_path[len(prefix) :]

        # Code based on diffusers.pipelines.pipeline_utils.DiffusionPipeline.from_pretrained
        ckpt_path = Path(pretrained_model_link_or_path)
        if not ckpt_path.is_file():
            # get repo_id and (potentially nested) file path of ckpt in repo
            repo_id = "/".join(ckpt_path.parts[:2])
            file_path = "/".join(ckpt_path.parts[2:])

            if file_path.startswith("blob/"):
                file_path = file_path[len("blob/") :]

            if file_path.startswith("main/"):
                file_path = file_path[len("main/") :]

            pretrained_model_link_or_path = hf_hub_download(
                repo_id,
                filename=file_path,
                cache_dir=cache_dir,
                resume_download=resume_download,
                proxies=proxies,
                local_files_only=local_files_only,
                token=token,
                revision=revision,
                force_download=force_download,
            )

        if config_file is None:
            config_url = "https://raw.githubusercontent.com/lllyasviel/ControlNet/main/models/cldm_v15.yaml"
            config_file = BytesIO(requests.get(config_url).content)

        image_size = image_size or 512

        controlnet = download_controlnet_from_original_ckpt(
            pretrained_model_link_or_path,
            original_config_file=config_file,
            image_size=image_size,
            extract_ema=extract_ema,
            num_in_channels=num_in_channels,
            upcast_attention=upcast_attention,
            from_safetensors=from_safetensors,
            use_linear_projection=use_linear_projection,
        )

        if torch_dtype is not None:
            controlnet.to(dtype=torch_dtype)

        return controlnet
