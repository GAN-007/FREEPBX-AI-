"""
Milestone7 pipeline orchestrator and placeholder component adapters.

This module introduces the PipelineOrchestrator that resolves STT/LLM/TTS
component adapters per configured pipeline. Components that are not yet
implemented are represented by placeholder adapters that transparently raise
NotImplementedError when invoked. Phase 4 will replace these placeholders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..config import (
    AppConfig,
    PipelineEntry,
    DeepgramProviderConfig,
    GoogleProviderConfig,
    LocalProviderConfig,
    OpenAIProviderConfig,
)
from ..logging_config import get_logger
from .base import Component, STTComponent, LLMComponent, TTSComponent
from .deepgram import DeepgramSTTAdapter, DeepgramTTSAdapter
from .deepgram_flux import DeepgramFluxSTTAdapter
from .google import GoogleLLMAdapter, GoogleSTTAdapter, GoogleTTSAdapter
from .local import LocalLLMAdapter, LocalSTTAdapter, LocalTTSAdapter
from .openai import OpenAISTTAdapter, OpenAILLMAdapter, OpenAITTSAdapter

logger = get_logger(__name__)

ComponentFactory = Callable[[str, Dict[str, Any]], Component]


class PipelineOrchestratorError(Exception):
    """Raised when the pipeline orchestrator cannot resolve components."""


@dataclass
class PipelineResolution:
    """Milestone7: Snapshot of the STT/LLM/TTS adapters assigned to a call."""
    call_id: str
    pipeline_name: str
    stt_key: str
    stt_adapter: STTComponent
    stt_options: Dict[str, Any]
    llm_key: str
    llm_adapter: LLMComponent
    llm_options: Dict[str, Any]
    tts_key: str
    tts_adapter: TTSComponent
    tts_options: Dict[str, Any]
    primary_provider: Optional[str] = None
    prepared: bool = False

    def component_summary(self) -> Dict[str, str]:
        return {
            "stt": self.stt_key,
            "llm": self.llm_key,
            "tts": self.tts_key,
        }

    def options_summary(self) -> Dict[str, Dict[str, Any]]:
        return {
            "stt": self.stt_options,
            "llm": self.llm_options,
            "tts": self.tts_options,
        }


class _PlaceholderBase:
    """# Milestone7: Shared helper for placeholder adapters."""

    def __init__(self, component_key: str, options: Optional[Dict[str, Any]] = None):
        self.component_key = component_key
        self.options = options or {}

    def __repr__(self) -> str:
        return f"<PlaceholderComponent key={self.component_key}>"


class PlaceholderSTTAdapter(STTComponent, _PlaceholderBase):
    """# Milestone7: Placeholder STT adapter awaiting concrete implementation."""

    def __init__(self, component_key: str, options: Optional[Dict[str, Any]] = None):
        _PlaceholderBase.__init__(self, component_key, options)

    async def transcribe(
        self,
        call_id: str,
        audio_pcm16: bytes,
        sample_rate_hz: int,
        options: Dict[str, Any],
    ) -> str:
        raise NotImplementedError(
            f"# Milestone7 placeholder STT adapter '{self.component_key}' is not implemented yet."
        )


class PlaceholderLLMAdapter(LLMComponent, _PlaceholderBase):
    """# Milestone7: Placeholder LLM adapter awaiting concrete implementation."""

    def __init__(self, component_key: str, options: Optional[Dict[str, Any]] = None):
        _PlaceholderBase.__init__(self, component_key, options)

    async def generate(
        self,
        call_id: str,
        transcript: str,
        context: Dict[str, Any],
        options: Dict[str, Any],
    ) -> str:
        raise NotImplementedError(
            f"# Milestone7 placeholder LLM adapter '{self.component_key}' is not implemented yet."
        )


class PlaceholderTTSAdapter(TTSComponent, _PlaceholderBase):
    """# Milestone7: Placeholder TTS adapter awaiting concrete implementation."""

    def __init__(self, component_key: str, options: Optional[Dict[str, Any]] = None):
        _PlaceholderBase.__init__(self, component_key, options)

    async def synthesize(
        self,
        call_id: str,
        text: str,
        options: Dict[str, Any],
    ):
        raise NotImplementedError(
            f"# Milestone7 placeholder TTS adapter '{self.component_key}' is not implemented yet."
        )


_PLACEHOLDER_CLASS_BY_ROLE: Dict[str, Callable[[str, Dict[str, Any]], Component]] = {
    "stt": PlaceholderSTTAdapter,
    "llm": PlaceholderLLMAdapter,
    "tts": PlaceholderTTSAdapter,
}


def _extract_role(component_key: str) -> str:
    parts = component_key.rsplit("_", 1)
    if len(parts) != 2:
        raise PipelineOrchestratorError(f"Cannot determine component role for '{component_key}'")
    return parts[1]


def _extract_provider(component_key: str) -> Optional[str]:
    parts = component_key.rsplit("_", 1)
    if len(parts) != 2:
        return None
    return parts[0]


def _make_placeholder_factory(role: str) -> ComponentFactory:
    adapter_cls = _PLACEHOLDER_CLASS_BY_ROLE.get(role)
    if adapter_cls is None:
        raise PipelineOrchestratorError(f"No placeholder adapter registered for role '{role}'")

    def factory(component_key: str, options: Dict[str, Any]) -> Component:
        return adapter_cls(component_key, options)

    return factory


def _build_default_registry() -> Dict[str, ComponentFactory]:
    registry: Dict[str, ComponentFactory] = {}
    default_providers = (
        "local",
        "deepgram",
        "openai",
        "openai_realtime",
        "google",
    )

    for provider in default_providers:
        for role in ("stt", "llm", "tts"):
            key = f"{provider}_{role}"
            registry[key] = _make_placeholder_factory(role)

    for role in ("stt", "llm", "tts"):
        registry[f"*_{role}"] = _make_placeholder_factory(role)

    return registry


DEFAULT_COMPONENT_REGISTRY = _build_default_registry()


class PipelineOrchestrator:
    """# Milestone7: Resolve STT/LLM/TTS adapters for calls based on pipeline config."""

    def __init__(
        self,
        config: AppConfig,
        *,
        registry: Optional[Dict[str, ComponentFactory]] = None,
    ):
        self.config = config
        self._registry: Dict[str, ComponentFactory] = dict(DEFAULT_COMPONENT_REGISTRY)
        if registry:
            self._registry.update(registry)

        self._local_provider_config: Optional[LocalProviderConfig] = self._hydrate_local_config()
        self._deepgram_provider_config: Optional[DeepgramProviderConfig] = self._hydrate_deepgram_config()
        self._openai_provider_config: Optional[OpenAIProviderConfig] = self._hydrate_openai_config()
        self._google_provider_config: Optional[GoogleProviderConfig] = self._hydrate_google_config()
        self._register_builtin_factories()

        self._assignments: Dict[str, PipelineResolution] = {}
        self._started: bool = False
        self._enabled: bool = bool(getattr(config, "pipelines", {}) or {})
        self._active_pipeline_name: Optional[str] = getattr(config, "active_pipeline", None)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Pipeline orchestrator disabled - no pipelines configured.")
            return

        pipelines = getattr(self.config, "pipelines", {}) or {}
        
        # Phase 1: Validate factory existence
        for name, entry in pipelines.items():
            self._validate_pipeline_entry(name, entry)
        
        # Phase 2: Validate connectivity for all pipelines
        validation_results = {}
        for name, entry in pipelines.items():
            validation_results[name] = await self._validate_pipeline_connectivity(name, entry)
        
        # Check if active pipeline is healthy
        active_healthy = True
        if self._active_pipeline_name:
            active_result = validation_results.get(self._active_pipeline_name, {})
            active_healthy = active_result.get("healthy", False)
            if not active_healthy:
                logger.error(
                    "Active pipeline validation FAILED - pipeline disabled",
                    pipeline=self._active_pipeline_name,
                    failures=active_result.get("failures", []),
                )
                self._active_pipeline_name = None
        
        # Log summary
        healthy_count = sum(1 for r in validation_results.values() if r.get("healthy"))
        unhealthy_count = len(validation_results) - healthy_count
        
        self._started = True
        logger.info(
            "Pipeline orchestrator initialized",
            active_pipeline=self._active_pipeline_name,
            pipeline_count=len(pipelines),
            healthy_pipelines=healthy_count,
            unhealthy_pipelines=unhealthy_count,
        )

    async def stop(self) -> None:
        if not self._started:
            return

        for call_id in list(self._assignments.keys()):
            await self.release_pipeline(call_id)

        self._started = False
        logger.info("Pipeline orchestrator stopped", remaining_assignments=len(self._assignments))

    def get_pipeline(
        self,
        call_id: str,
        pipeline_name: Optional[str] = None,
    ) -> Optional[PipelineResolution]:
        if not self.enabled:
            return None
        if not self._started:
            logger.debug("Pipeline orchestrator requested before start; skipping resolution", call_id=call_id)
            return None

        if call_id in self._assignments:
            return self._assignments[call_id]

        pipelines = getattr(self.config, "pipelines", {}) or {}
        selected_name = pipeline_name or self._active_pipeline_name

        if not selected_name:
            try:
                selected_name = next(iter(pipelines.keys()))
            except StopIteration:
                logger.error("No pipelines available to assign", call_id=call_id)
                return None

        entry = pipelines.get(selected_name)
        if entry is None:
            logger.warning(
                "Requested pipeline not found; falling back to first available pipeline",
                call_id=call_id,
                requested_pipeline=selected_name,
            )
            try:
                selected_name, entry = next(iter(pipelines.items()))
            except StopIteration:
                return None

        resolution = self._build_resolution(call_id, selected_name, entry)
        self._assignments[call_id] = resolution
        return resolution

    async def release_pipeline(self, call_id: str) -> None:
        resolution = self._assignments.pop(call_id, None)
        if not resolution:
            return

        for adapter in (resolution.stt_adapter, resolution.llm_adapter, resolution.tts_adapter):
            await self._shutdown_component(adapter, call_id)

    def register_factory(self, component_key: str, factory: ComponentFactory) -> None:
        self._registry[component_key] = factory

    def _hydrate_local_config(self) -> Optional[LocalProviderConfig]:
        providers = getattr(self.config, "providers", {}) or {}
        raw_config = providers.get("local")
        if not raw_config:
            return None
        if isinstance(raw_config, LocalProviderConfig):
            cfg = raw_config
        elif isinstance(raw_config, dict):
            enabled = raw_config.get("enabled", True)
            if not enabled:
                logger.debug("Local provider disabled via configuration")
                return None
            try:
                cfg = LocalProviderConfig(**raw_config)
            except Exception as exc:
                logger.warning(
                    "Failed to hydrate Local provider config for pipelines",
                    error=str(exc),
                )
                return None
        else:
            logger.warning(
                "Unsupported Local provider config type for pipelines",
                config_type=type(raw_config).__name__,
            )
            return None

        if not cfg.enabled:
            logger.debug("Local provider disabled after hydration")
            return None

        return cfg

    def _hydrate_deepgram_config(self) -> Optional[DeepgramProviderConfig]:
        providers = getattr(self.config, "providers", {}) or {}
        raw_config = providers.get("deepgram")
        if not raw_config:
            return None
        if isinstance(raw_config, DeepgramProviderConfig):
            return raw_config
        if isinstance(raw_config, dict):
            try:
                return DeepgramProviderConfig(**raw_config)
            except Exception as exc:
                logger.warning(
                    "Failed to hydrate Deepgram provider config for pipelines",
                    error=str(exc),
                )
                return None
        logger.warning(
            "Unsupported Deepgram provider config type for pipelines",
            config_type=type(raw_config).__name__,
        )
        return None

    def _register_builtin_factories(self) -> None:
        if self._local_provider_config:
            stt_factory = self._make_local_stt_factory(self._local_provider_config)
            llm_factory = self._make_local_llm_factory(self._local_provider_config)
            tts_factory = self._make_local_tts_factory(self._local_provider_config)

            self.register_factory("local_stt", stt_factory)
            self.register_factory("local_llm", llm_factory)
            self.register_factory("local_tts", tts_factory)

            logger.info(
                "Local pipeline adapters registered",
                stt_factory="local_stt",
                llm_factory="local_llm",
                tts_factory="local_tts",
            )
        else:
            logger.debug("Local pipeline adapters not registered - provider config unavailable or disabled")

        if self._deepgram_provider_config:
            stt_factory = self._make_deepgram_stt_factory(self._deepgram_provider_config)
            flux_stt_factory = self._make_deepgram_flux_stt_factory(self._deepgram_provider_config)
            tts_factory = self._make_deepgram_tts_factory(self._deepgram_provider_config)

            self.register_factory("deepgram_stt", stt_factory)
            self.register_factory("deepgram_flux_stt", flux_stt_factory)
            self.register_factory("deepgram_tts", tts_factory)

            logger.info(
                "Deepgram pipeline adapters registered",
                stt_factory="deepgram_stt",
                flux_stt_factory="deepgram_flux_stt",
                tts_factory="deepgram_tts",
            )
        else:
            logger.debug("Deepgram pipeline adapters not registered - provider config unavailable")

        if self._openai_provider_config:
            stt_factory = self._make_openai_stt_factory(self._openai_provider_config)
            llm_factory = self._make_openai_llm_factory(self._openai_provider_config)
            tts_factory = self._make_openai_tts_factory(self._openai_provider_config)

            self.register_factory("openai_stt", stt_factory)
            self.register_factory("openai_llm", llm_factory)
            self.register_factory("openai_tts", tts_factory)

            logger.info(
                "OpenAI pipeline adapters registered",
                stt_factory="openai_stt",
                llm_factory="openai_llm",
                tts_factory="openai_tts",
            )
        else:
            logger.debug("OpenAI pipeline adapters not registered - provider config unavailable or invalid")

        if self._google_provider_config:
            stt_factory = self._make_google_stt_factory(self._google_provider_config)
            llm_factory = self._make_google_llm_factory(self._google_provider_config)
            tts_factory = self._make_google_tts_factory(self._google_provider_config)

            self.register_factory("google_stt", stt_factory)
            self.register_factory("google_llm", llm_factory)
            self.register_factory("google_tts", tts_factory)

            logger.info(
                "Google pipeline adapters registered",
                stt_factory="google_stt",
                llm_factory="google_llm",
                tts_factory="google_tts",
            )
        else:
            logger.debug("Google pipeline adapters not registered - credentials unavailable or invalid")

    def _make_local_stt_factory(
        self,
        provider_config: LocalProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return LocalSTTAdapter(
                component_key,
                self.config,
                LocalProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_local_llm_factory(
        self,
        provider_config: LocalProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return LocalLLMAdapter(
                component_key,
                self.config,
                LocalProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_local_tts_factory(
        self,
        provider_config: LocalProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return LocalTTSAdapter(
                component_key,
                self.config,
                LocalProviderConfig(**config_payload),
                options,
            )

        return factory

    

    def _make_deepgram_stt_factory(
        self,
        provider_config: DeepgramProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return DeepgramSTTAdapter(
                component_key,
                self.config,
                DeepgramProviderConfig(**config_payload),
                options,
            )

        return factory
    
    def _make_deepgram_flux_stt_factory(
        self,
        provider_config: DeepgramProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return DeepgramFluxSTTAdapter(
                component_key,
                self.config,
                DeepgramProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_openai_stt_factory(
        self,
        provider_config: OpenAIProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return OpenAISTTAdapter(
                component_key,
                self.config,
                OpenAIProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_openai_llm_factory(
        self,
        provider_config: OpenAIProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return OpenAILLMAdapter(
                component_key,
                self.config,
                OpenAIProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_openai_tts_factory(
        self,
        provider_config: OpenAIProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return OpenAITTSAdapter(
                component_key,
                self.config,
                OpenAIProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_deepgram_tts_factory(
        self,
        provider_config: DeepgramProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return DeepgramTTSAdapter(
                component_key,
                self.config,
                DeepgramProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_google_stt_factory(
        self,
        provider_config: GoogleProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return GoogleSTTAdapter(
                component_key,
                self.config,
                GoogleProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_google_llm_factory(
        self,
        provider_config: GoogleProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return GoogleLLMAdapter(
                component_key,
                self.config,
                GoogleProviderConfig(**config_payload),
                options,
            )

        return factory

    def _make_google_tts_factory(
        self,
        provider_config: GoogleProviderConfig,
    ) -> ComponentFactory:
        config_payload = provider_config.model_dump()

        def factory(component_key: str, options: Dict[str, Any]) -> Component:
            return GoogleTTSAdapter(
                component_key,
                self.config,
                GoogleProviderConfig(**config_payload),
                options,
            )

        return factory

    def _hydrate_google_config(self) -> Optional[GoogleProviderConfig]:
        providers = getattr(self.config, "providers", {}) or {}
        raw_config = providers.get("google")
        if not raw_config:
            return None
        if isinstance(raw_config, GoogleProviderConfig):
            config = raw_config
        elif isinstance(raw_config, dict):
            try:
                config = GoogleProviderConfig(**raw_config)
            except Exception as exc:
                logger.warning(
                    "Failed to hydrate Google provider config for pipelines",
                    error=str(exc),
                )
                return None
        else:
            logger.warning(
                "Unsupported Google provider config type for pipelines",
                config_type=type(raw_config).__name__,
            )
            return None

        if not (
            config.api_key
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "Google pipeline adapters require GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS; falling back to placeholder adapters",
            )
            return None

        return config

    def _hydrate_openai_config(self) -> Optional[OpenAIProviderConfig]:
        providers = getattr(self.config, "providers", {}) or {}
        raw_config = providers.get("openai")
        if not raw_config:
            return None
        if isinstance(raw_config, OpenAIProviderConfig):
            config = raw_config
        elif isinstance(raw_config, dict):
            try:
                config = OpenAIProviderConfig(**raw_config)
            except Exception as exc:
                logger.warning(
                    "Failed to hydrate OpenAI provider config for pipelines",
                    error=str(exc),
                )
                return None
        else:
            logger.warning(
                "Unsupported OpenAI provider config type for pipelines",
                config_type=type(raw_config).__name__,
            )
            return None

        if not config.api_key:
            logger.warning("OpenAI pipeline adapters require an API key; falling back to placeholder adapters")
            return None

        return config

    def _resolve_factory(self, component_key: str) -> ComponentFactory:
        factory = self._registry.get(component_key)
        if factory:
            return factory

        role = _extract_role(component_key)
        wildcard_key = f"*_{role}"
        factory = self._registry.get(wildcard_key)
        if factory:
            # Cache the wildcard resolution for quicker lookups next time.
            self._registry[component_key] = factory
            return factory

        raise PipelineOrchestratorError(f"No component factory registered for '{component_key}'")

    def _build_component(self, component_key: str, options: Dict[str, Any]) -> Component:
        factory = self._resolve_factory(component_key)
        return factory(component_key, options)

    def _derive_primary_provider(self, entry: PipelineEntry) -> Optional[str]:
        for key in (entry.llm, entry.tts, entry.stt):
            provider = _extract_provider(key)
            if provider:
                return provider
        return None

    def _validate_pipeline_entry(self, pipeline_name: str, entry: PipelineEntry) -> None:
        """Validate that component factories exist (static check)."""
        for key in (entry.stt, entry.llm, entry.tts):
            self._resolve_factory(key)
    
    async def _validate_pipeline_connectivity(self, pipeline_name: str, entry: PipelineEntry) -> Dict[str, Any]:
        """Validate pipeline components can connect to required services.
        
        Returns:
            Dict with:
                - healthy: bool
                - failures: List[Dict] - Component failure details
        """
        failures = []
        options_map = entry.options or {}
        
        # Validate STT
        try:
            stt_options = dict(options_map.get("stt", {}))
            stt_adapter = self._build_component(entry.stt, stt_options)
            result = await stt_adapter.validate_connectivity(stt_options)
            if not result.get("healthy"):
                failures.append({
                    "component": "stt",
                    "key": entry.stt,
                    "error": result.get("error"),
                    "details": result.get("details", {}),
                })
                logger.error(
                    "Pipeline STT validation FAILED",
                    pipeline=pipeline_name,
                    component_key=entry.stt,
                    error=result.get("error"),
                    details=result.get("details", {}),
                )
        except Exception as exc:
            failures.append({
                "component": "stt",
                "key": entry.stt,
                "error": f"Validation exception: {exc}",
                "details": {},
            })
            logger.error(
                "Pipeline STT validation exception",
                pipeline=pipeline_name,
                component_key=entry.stt,
                exc_info=True,
            )
        
        # Validate LLM
        try:
            llm_options = dict(options_map.get("llm", {}))
            llm_adapter = self._build_component(entry.llm, llm_options)
            result = await llm_adapter.validate_connectivity(llm_options)
            if not result.get("healthy"):
                failures.append({
                    "component": "llm",
                    "key": entry.llm,
                    "error": result.get("error"),
                    "details": result.get("details", {}),
                })
                logger.error(
                    "Pipeline LLM validation FAILED",
                    pipeline=pipeline_name,
                    component_key=entry.llm,
                    error=result.get("error"),
                    details=result.get("details", {}),
                )
        except Exception as exc:
            failures.append({
                "component": "llm",
                "key": entry.llm,
                "error": f"Validation exception: {exc}",
                "details": {},
            })
            logger.error(
                "Pipeline LLM validation exception",
                pipeline=pipeline_name,
                component_key=entry.llm,
                exc_info=True,
            )
        
        # Validate TTS
        try:
            tts_options = dict(options_map.get("tts", {}))
            tts_adapter = self._build_component(entry.tts, tts_options)
            result = await tts_adapter.validate_connectivity(tts_options)
            if not result.get("healthy"):
                failures.append({
                    "component": "tts",
                    "key": entry.tts,
                    "error": result.get("error"),
                    "details": result.get("details", {}),
                })
                logger.error(
                    "Pipeline TTS validation FAILED",
                    pipeline=pipeline_name,
                    component_key=entry.tts,
                    error=result.get("error"),
                    details=result.get("details", {}),
                )
        except Exception as exc:
            failures.append({
                "component": "tts",
                "key": entry.tts,
                "error": f"Validation exception: {exc}",
                "details": {},
            })
            logger.error(
                "Pipeline TTS validation exception",
                pipeline=pipeline_name,
                component_key=entry.tts,
                exc_info=True,
            )
        
        healthy = len(failures) == 0
        if healthy:
            logger.info(
                "Pipeline validation SUCCESS",
                pipeline=pipeline_name,
                components={"stt": entry.stt, "llm": entry.llm, "tts": entry.tts},
            )
        
        return {"healthy": healthy, "failures": failures}

    def _build_resolution(
        self,
        call_id: str,
        pipeline_name: str,
        entry: PipelineEntry,
    ) -> PipelineResolution:
        options_map = entry.options or {}
        stt_options = dict(options_map.get("stt", {}))
        llm_options = dict(options_map.get("llm", {}))
        tts_options = dict(options_map.get("tts", {}))

        # Milestone7: Inject tools into LLM options if configured
        if hasattr(entry, "tools") and entry.tools:
            llm_options["tools"] = entry.tools

        stt_adapter = self._build_component(entry.stt, stt_options)
        llm_adapter = self._build_component(entry.llm, llm_options)
        tts_adapter = self._build_component(entry.tts, tts_options)

        primary_provider = self._derive_primary_provider(entry)

        return PipelineResolution(
            call_id=call_id,
            pipeline_name=pipeline_name,
            stt_key=entry.stt,
            stt_adapter=stt_adapter,
            stt_options=stt_options,
            llm_key=entry.llm,
            llm_adapter=llm_adapter,
            llm_options=llm_options,
            tts_key=entry.tts,
            tts_adapter=tts_adapter,
            tts_options=tts_options,
            primary_provider=primary_provider,
        )

    async def _shutdown_component(self, component: Component, call_id: str) -> None:
        try:
            await component.close_call(call_id)
        except NotImplementedError:
            logger.debug(
                "Placeholder component close_call not implemented",
                call_id=call_id,
                component_key=getattr(component, "component_key", repr(component)),
            )
        except Exception as exc:
            logger.warning(
                "Pipeline component close_call failed",
                call_id=call_id,
                component_key=getattr(component, "component_key", repr(component)),
                error=str(exc),
                exc_info=True,
            )

        try:
            await component.stop()
        except NotImplementedError:
            logger.debug(
                "Placeholder component stop not implemented",
                call_id=call_id,
                component_key=getattr(component, "component_key", repr(component)),
            )
        except Exception as exc:
            logger.warning(
                "Pipeline component stop failed",
                call_id=call_id,
                component_key=getattr(component, "component_key", repr(component)),
                error=str(exc),
                exc_info=True,
            )


__all__ = [
    "PipelineOrchestrator",
    "PipelineResolution",
    "PipelineOrchestratorError",
]