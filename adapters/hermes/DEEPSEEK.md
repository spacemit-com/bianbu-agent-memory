# DeepSeek + Hermes v0.21.7 + Bianbu Memory

DeepSeek is the model provider in this integration. Hermes owns the chat
lifecycle; the existing Bianbu MemoryProvider delegates extraction, validation,
deduplication and recall to the same SDK Runtime bridge used with MiniMax.
There is no separate DeepSeek memory algorithm or change to Hermes core.

## K3 usage

The sample `hermes-memory-v0217-deepseek` shell entry point belongs in
`/home/example/.local/bin/`. It uses the same pinned source/interpreter as the
Hermes adapter and a separate profile:
`~/.local/state/hermes-memory-v0217-deepseek/hermes`.

On first launch, the generic development launcher creates model configuration
for provider `deepseek`, model `deepseek-flash`, and URL
`https://api.deepseek.com/v1`. Existing configuration is preserved. Configure
credentials using this profile's model wizard:

```bash
/home/example/.local/bin/hermes-memory-v0217-deepseek model
/home/example/.local/bin/hermes-memory-v0217-deepseek chat
```

Alternatively, export `DEEPSEEK_API_KEY` in the shell launching Hermes. The
launcher never copies credentials or writes them to model config. A private
profile `.env` can be managed by Hermes. Keep credentials out of Git and logs.
Hermes v0.21.7 remaps retired `deepseek-chat` and `deepseek-reasoner` names to
`deepseek-flash`. For a compatible local endpoint, use the Python launcher's
`--model-provider`, `--model` and `--model-base-url` options with another fresh
state directory and the appropriate Hermes provider configuration.

CLI and Dashboard share the memory adapter. For a separate Dashboard profile,
use the service example in README with a different state directory and port,
plus the model options above. The existing MiniMax Dashboard is preserved.

## Automated K3 gate

```bash
/home/example/projects/hermes-agent-base/.venv/bin/python \
  /home/example/mem-gateway-build/scripts/hermes_v0217_acceptance.py \
  --hermes-source /home/example/projects/hermes-agent-memory-v0.21.7 \
  --sdk-path /usr/lib/python3/dist-packages \
  --model-provider deepseek --model deepseek-flash \
  --model-base-url https://api.deepseek.com/v1 \
  --report /home/example/hermes-deepseek-v0217-acceptance.json
```

Repeat with `--model deepseek-reasoner --expected-model deepseek-flash` to
verify the legacy alias normalization. Reasoning effort is configured through
Hermes controls; the alias itself does not enable reasoning. Each run uses the
full real memoryd gate,
including automatic preference extraction without a memory tool invocation,
standing preference recall, provider recreation, explicit-write validation,
deduplication and shutdown. It also asserts AIAgent's provider/model/base URL
and loads the real memory provider. It makes zero model requests: direct
MemoryManager turns test the memory lifecycle, not the model's chat API,
reasoning replay or tool-calling behavior. Reports record these limits.

Live acceptance requires a valid DeepSeek key: ask to remember a unique name
and the preference "以后说话要通俗易懂", finish the turn, start a new session,
then ask your name and an unrelated explanation. Verify backend records as
well as replies. Model authentication failure must be diagnosed separately
from memory persistence. No valid key was available in the checked K3
environment/profiles, so cloud chat acceptance remains pending.

The profile uses isolated development identity, encrypted persistent storage
and lexical recall. Production identity registration and semantic embedding
acceptance are outside this gate. No production socket/data or port 18800
instance is modified. Do not change `debian/changelog` for this integration.
