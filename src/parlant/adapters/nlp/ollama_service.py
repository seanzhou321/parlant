# Copyright 2024 Emcie Co Ltd.
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

from __future__ import annotations
import time
import json
import jsonfinder  # type: ignore
import os
from typing import Any, Mapping
from typing_extensions import override

import ollama
from pydantic import ValidationError, BaseModel
import tiktoken

from parlant.adapters.nlp.common import normalize_json_output
from parlant.adapters.nlp.hugging_face import JinaAIEmbedder
from parlant.core.engines.alpha.prompt_builder import PromptBuilder
from parlant.core.loggers import Logger
from parlant.core.nlp.policies import policy, retry
from parlant.core.nlp.tokenization import EstimatingTokenizer
from parlant.core.nlp.service import NLPService
from parlant.core.nlp.embedding import Embedder
from parlant.core.nlp.generation import (
    T,
    SchematicGenerator,
    SchematicGenerationResult,
)
from parlant.core.nlp.generation_info import GenerationInfo, UsageInfo
from parlant.core.nlp.moderation import (
    ModerationService,
    NoModeration,
)


class OllamaEstimatingTokenizer(EstimatingTokenizer):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        # Map Ollama models to closest OpenAI models for tokenization
        model_encoding_map = {
            "llama2": "gpt-4",
            "codellama": "gpt-4",
            "mistral": "gpt-3.5-turbo",
            "neural-chat": "gpt-3.5-turbo",
            "qwen2": "gpt-4",          # Qwen-2.5 uses a similar architecture to GPT-4
            "deepseek": "gpt-4",       # DeepSeek-R1 is similar to GPT-4 in capabilities
            "phi": "gpt-3.5-turbo",    # Phi-4 is closer to GPT-3.5 in size and capabilities
        }
        base_model = model_name.split(":")[0]
        encoding_model = model_encoding_map.get(base_model, "gpt-4")
        self.encoding = tiktoken.encoding_for_model(encoding_model)

    @override
    async def estimate_token_count(self, prompt: str) -> int:
        tokens = self.encoding.encode(prompt)
        return len(tokens)


class OllamaSchematicGenerator(SchematicGenerator[T]):
    """Ollama-based schematic generator.
    
    Supported parameters:
    - temperature: Controls randomness (0.0 - 1.0)
    - num_predict: Maximum number of tokens to generate in response
    - top_k: Limits vocabulary to top K tokens (1-100)
    - top_p: Limits vocabulary by cumulative probability (0.0-1.0)
    - num_ctx: Maximum total tokens (prompt + response) that can be processed (default: 100K)
    - stop: Stop sequences that will end generation
    - seed: Random seed for reproducibility
    - mirostat: Enable Mirostat sampling control (0, 1, 2)
    - mirostat_eta: Learning rate for Mirostat
    - mirostat_tau: Target entropy for Mirostat
    - num_gpu: Number of GPUs to use
    - num_thread: Number of CPU threads to use
    """
    supported_models = {
        "llama2", "codellama", "mistral", "neural-chat",
        "qwen2", "deepseek", "phi"
    }
    # Update supported parameters to match Ollama's API
    supported_ollama_params = [
        "temperature",  
        "num_predict",  # Replace max_tokens with num_predict
        "top_k",
        "top_p",
        "num_ctx",
        "stop",
        "seed",
        "mirostat",
        "mirostat_eta",
        "mirostat_tau",
        "num_gpu",
        "num_thread"
    ]
    supported_hints = supported_ollama_params + ["strict"]

    def __init__(
        self,
        model_name: str,
        logger: Logger,
        config: OllamaConfig,
    ) -> None:
        base_model = model_name.split(":")[0]
        if base_model not in self.supported_models:
            logger.warning(f"Model {model_name} not in list of known supported models: {self.supported_models}")
        self.model_name = model_name
        self._logger = logger
        self._config = config
        
        # Configure ollama client with base URL
        ollama.set_host(self._config.base_url)
        self._tokenizer = OllamaEstimatingTokenizer(model_name=self.model_name)

    @property
    @override
    def id(self) -> str:
        return f"ollama/{self.model_name}"

    @property
    @override
    def tokenizer(self) -> OllamaEstimatingTokenizer:
        return self._tokenizer

    @policy(
        [
            retry(
                exceptions=(
                    ollama.RequestError,  # Connection errors
                    ollama.TimeoutError,  # Timeout errors
                ),
            ),
        ]
    )
    @override
    async def generate(
        self,
        prompt: str | PromptBuilder,
        hints: Mapping[str, Any] = {},
    ) -> SchematicGenerationResult[T]:
        with self._logger.operation(f"Ollama LLM Request ({self.schema.__name__})"):
            return await self._do_generate(prompt, hints)

    async def _do_generate(
        self,
        prompt: str | PromptBuilder,
        hints: Mapping[str, Any] = {},
    ) -> SchematicGenerationResult[T]:
        try:
            if isinstance(prompt, PromptBuilder):
                prompt = prompt.build()

            # Set default arguments including num_ctx
            api_arguments = {
                "num_ctx": 100 * 1024,  # Set default context window to 100K tokens
            }
            
            # Add user-provided arguments
            for k, v in hints.items():
                if k == "max_tokens":
                    api_arguments["num_predict"] = v
                elif k in self.supported_ollama_params:
                    api_arguments[k] = v

            t_start = time.time()
            response = await ollama.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                format="json",
                **api_arguments
            )
            t_end = time.time()

            raw_content = response['message']['content']

            try:
                json_content = json.loads(normalize_json_output(raw_content))
            except json.JSONDecodeError:
                self._logger.warning(f"Invalid JSON returned by {self.model_name}:\n{raw_content})")
                try:
                    json_matches = jsonfinder.only_json(raw_content)
                    if not json_matches:
                        raise ValueError("No valid JSON found in response")
                    json_content = json_matches[2]
                    self._logger.warning("Found JSON content within model response; continuing...")
                except Exception as e:
                    raise ValueError(f"Failed to parse response as JSON: {str(e)}") from e

            try:
                content = self.schema.model_validate(json_content)

                assert response.get('usage')

                return SchematicGenerationResult(
                    content=content,
                    info=GenerationInfo(
                        schema_name=self.schema.__name__,
                        model=self.id,
                        duration=(t_end - t_start),
                        usage=UsageInfo(
                            input_tokens=response['usage']['prompt_tokens'],
                            output_tokens=response['usage']['completion_tokens'],
                            extra={
                                "cached_input_tokens": response['usage'].get(
                                    "prompt_cache_hit_tokens",
                                    0,
                                )
                            },
                        ),
                    ),
                )
            except ValidationError:
                self._logger.error(
                    f"JSON content returned by {self.model_name} does not match expected schema:\n{raw_content}"
                )
                raise
        except ollama.APIConnectionError:
            raise ollama.APIConnectionError(
                f"Failed to connect to Ollama server at {self._config.base_url}. "
                "Is Ollama running?"
            )
        except Exception as e:
            self._logger.error(f"Ollama request failed: {str(e)}")
            raise


class Ollama_Chat(OllamaSchematicGenerator[T]):
    def __init__(self, logger: Logger, config: OllamaConfig | None = None) -> None:
        config = config or OllamaConfig.from_env()
        super().__init__(
            model_name=config.model_name,
            logger=logger,
            config=config
        )

    @property
    @override
    def max_tokens(self) -> int:
        return 128 * 1024


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    timeout: float = 60.0
    model_name: str = "llama2"

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", cls.base_url),
            timeout=float(os.getenv("OLLAMA_TIMEOUT", cls.timeout)),
            model_name=os.getenv("OLLAMA_MODEL", cls.model_name)
        )


class OllamaService(NLPService):
    def __init__(
        self,
        logger: Logger,
        config: OllamaConfig | None = None,
    ) -> None:
        self._logger = logger
        self._config = config or OllamaConfig.from_env()
        self._logger.info(f"Initialized OllamaService with model: {self._config.model_name}")

    @override
    async def get_schematic_generator(self, t: type[T]) -> OllamaSchematicGenerator[T]:
        return Ollama_Chat[t](self._logger)  # type: ignore

    @override
    async def get_embedder(self) -> Embedder:
        return JinaAIEmbedder()

    @override
    async def get_moderation_service(self) -> ModerationService:
        return NoModeration()
