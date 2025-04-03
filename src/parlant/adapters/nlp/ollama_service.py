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
from typing import Any, Mapping, Dict, Optional
from typing_extensions import override

import ollama
from pydantic import ValidationError, BaseModel

from parlant.adapters.nlp.common import normalize_json_output
from parlant.adapters.nlp.hugging_face import JinaAIEmbedder
from parlant.core.engines.alpha.prompt_builder import PromptBuilder
from parlant.core.loggers import Logger
from parlant.core.nlp.policies import policy, retry
from parlant.core.nlp.tokenization import EstimatingTokenizer
from parlant.core.nlp.service import NLPService
from parlant.core.nlp.embedding import Embedder, EmbeddingResult
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
    def __init__(self, model_name: str, config: OllamaConfig) -> None:
        self.model_name = model_name
        self._config = config
        self._client = ollama.Client(host=config.base_url)
        # Cache for tokenization results
        self._token_cache: Dict[str, int] = {}

    @override
    async def estimate_token_count(self, prompt: str) -> int:
        if prompt in self._token_cache:
            return self._token_cache[prompt]
        
        try:
            # ollama client does not have tokenize. 
            # Instead, the response from generate or chat has prompt_eval_count field 
            # that is the number of tokens in the prompt
            result = self._client.generate(
                model=self.model_name,
                prompt=prompt
            )
            token_count = result['prompt_eval_count']
            self._token_cache[prompt] = token_count
            return token_count
        except Exception as e:
            self._logger.warning(f"Failed to tokenize using Ollama: {str(e)}")
            # Fallback to rough estimation - 4 characters per token
            return len(prompt) // 4


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

    # gemma3 does not support embedding.
    supported_models = {
        "llama3.2", "qwen2.5", "deepseek-r1", "phi4-mini"
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
        
        self._client = ollama.Client(host=config.base_url)
        self._tokenizer = OllamaEstimatingTokenizer(
            model_name=self.model_name,
            config=config
        )

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
                    Exception,  # Ollama package doesn't expose specific error types
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
                
            # Add token count check
            token_count = await self.tokenizer.estimate_token_count(prompt)
            if token_count > model_arguments.get('num_ctx', 100000):
                raise ValueError(f"Prompt too long ({token_count} tokens). Maximum context size is {model_arguments.get('num_ctx')}")

            # Set default arguments including num_ctx
            model_arguments = {
                'num_ctx': 1000000,  # Default context size for Ollama
            }
            
            # Add user-provided arguments
            for k, v in hints.items():
                if k == "max_tokens":
                    model_arguments["num_predict"] = v
                elif k in self.supported_ollama_params:
                    model_arguments[k] = v

            t_start = time.time()
            response = self._client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                format="json",
                options=model_arguments,
                timeout=self._config.timeout  # Add timeout from config
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

                return SchematicGenerationResult(
                    content=content,
                    info=GenerationInfo(
                        schema_name=self.schema.__name__,
                        model=self.id,
                        duration=(t_end - t_start),
                        usage=UsageInfo(
                            input_tokens=response['prompt_eval_count'],
                            output_tokens=response['eval_count'],
                        ),
                    ),
                )
            except ValidationError:
                self._logger.error(
                    f"JSON content returned by {self.model_name} does not match expected schema:\n{raw_content}"
                )
                raise
        except ConnectionError:
            raise ConnectionError(
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
        return 100 * 1024


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:10434"
    timeout: float = 60.0
    model_name: str = "llama3.2" 

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        return cls(
            base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            timeout=float(os.getenv("OLLAMA_TIMEOUT", "60.0")),
            model_name=os.getenv("OLLAMA_MODEL", "llama3.2")
        )
    

class OllamaEmbedder(Embedder):
    """Ollama-based embedder using the ollama.embed API."""
    
    def __init__(self, model_name: str, logger: Logger, config: OllamaConfig) -> None:
        self.model_name = model_name
        self._logger = logger
        self._config = config
        self._client = ollama.Client(host=config.base_url)
        self._tokenizer = OllamaEstimatingTokenizer(
            model_name=self.model_name,
            config=config
        )

    @property
    @override
    def id(self) -> str:
        return f"ollama/{self.model_name}"

    @property
    @override
    def tokenizer(self) -> OllamaEstimatingTokenizer:
        return self._tokenizer

    @property
    @override
    def max_tokens(self) -> int:
        return 8192  # Standard limit for most embedding models

    @property
    @override
    def dimensions(self) -> int:
        dimension_map = {
            "qwen2.5": 3584,
            "llama3.2": 4096,
            "deepseek-r1": 4096,
            "phi4-mini": 2560
        }
        
        # Extract base model name without tags/versions
        base_model = self.model_name.split(":")[0]
        
        # Get dimension for model or fall back to default
        dim = dimension_map.get(base_model)
        if dim is None:
            self._logger.warning(
                f"Unknown embedding dimensions for model {base_model}, "
                "falling back to default 4096"
            )
            dim = 4096
            
        return dim

    async def _chunk_and_embed(self, text: str, chunk_size: int = 8000) -> list[float]:
        # Split text into chunks if it's too long
        if len(text) > chunk_size:
            chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
            all_embeddings = []
            for chunk in chunks:
                response = self._client.embed(
                    model=self.model_name,
                    input=chunk
                )
                all_embeddings.extend(response['embeddings'])
            # Average the embeddings from all chunks
            return [sum(x)/len(chunks) for x in zip(*all_embeddings)]
        else:
            response = self._client.embed(
                model=self.model_name,
                input=text
            )
            return response['embeddings']

    @policy(
        [
            retry(
                exceptions=(
                    Exception,  # Ollama package doesn't expose specific error types
                ),
            ),
        ]
    )
    @override
    async def embed(
        self,
        texts: list[str],
        hints: Mapping[str, Any] = {},
    ) -> EmbeddingResult:
        try:
            vectors = []
            chunk_size = hints.get('chunk_size', 8000)
            
            for text in texts:
                vector = await self._chunk_and_embed(text, chunk_size)
                vectors.append(vector)
                
            return EmbeddingResult(vectors=vectors)
        except ConnectionError:
            raise ConnectionError(
                f"Failed to connect to Ollama server at {self._config.base_url}. "
                "Is Ollama running?"
            )
        except Exception as e:
            self._logger.error(f"Ollama embedding request failed: {str(e)}")
            raise


class OllamaDefaultEmbedder(OllamaEmbedder):
    def __init__(self, logger: Logger) -> None:
        config = OllamaConfig.from_env()
        super().__init__(
            model_name=config.model_name,
            logger=logger,
            config=config
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
        return Ollama_Chat[t](self._logger)

    @override
    async def get_embedder(self) -> Embedder:
        return OllamaDefaultEmbedder(self._logger)

    @override
    async def get_moderation_service(self) -> ModerationService:
        return NoModeration()
