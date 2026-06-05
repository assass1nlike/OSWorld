from desktop_env.providers.base import VMManager, Provider


_LEGACY_PROVIDERS = {
    "vmware",
    "virtualbox",
    "aws",
    "amazon web services",
    "azure",
    "docker",
    "aliyun",
    "gcp",
}


def create_vm_manager_and_provider(
    provider_name: str,
    region: str,
    use_proxy: bool = False,
    *,
    spec: str | None = None,
):
    """Factory for (VMManager, Provider) pairs.

    `msh-sandbox` and `volcengine` are migrated to the per-task-image interface
    (Provider.start_emulator(image, ...) / VMManager.resolve_image). Other
    providers raise an explicit error so failures are loud rather than silent.

    `spec` is provider-specific. For `msh-sandbox` it is the sandbox session
    spec name; for `volcengine` it can be used as ECS instance_type when the
    environment does not set VOLCENGINE_INSTANCE_TYPE.
    """
    name = provider_name.lower().strip()

    if name == "msh-sandbox":
        from desktop_env.providers.msh_sandbox.manager import MshSandboxVMManager
        from desktop_env.providers.msh_sandbox.provider import (
            DEFAULT_SPEC,
            MshSandboxProvider,
        )
        resolved_spec = spec if spec is not None else DEFAULT_SPEC
        return MshSandboxVMManager(), MshSandboxProvider(region, spec=resolved_spec)

    if name == "volcengine":
        from desktop_env.providers.volcengine.manager import VolcengineVMManager
        from desktop_env.providers.volcengine.provider import VolcengineProvider

        return VolcengineVMManager(), VolcengineProvider(region, spec=spec)

    if name in _LEGACY_PROVIDERS:
        raise NotImplementedError(
            f"Provider '{provider_name}' has not been migrated to the per-task-image "
            f"interface defined in desktop_env/providers/base.py. Only 'msh-sandbox' and 'volcengine' "
            f"is currently supported. To re-enable this provider, port its start_emulator/"
            f"get_ip_address/stop_emulator to the new no-path_to_vm signatures, replace "
            f"revert_to_snapshot with reset_to_clean, and implement VMManager.resolve_image "
            f"to map task_config['image'] to the provider-specific identifier."
        )

    raise NotImplementedError(f"{provider_name!r} not implemented!")
