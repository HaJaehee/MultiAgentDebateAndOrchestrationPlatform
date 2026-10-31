# Multimodal Input

An agent can receive the **content** of files, not just their paths. The card's **모달 N** button picks which formats
(`agents.<key>.allowed_modalities`); with every box empty the agent gets text and file paths, exactly as before.

Why it works this way: [ADR-035](../../lectures/05-adr/ADR-035-agent-input-modalities.md). Code: `app/agents/media.py`.

## 1. Formats

| Box | Value | Extensions | Goes to the model as |
| :--- | :--- | :--- | :--- |
| 이미지 | `image` | png, jpg, jpeg, webp, gif | `image_url` with a base64 data URI |
| 음성 | `audio` | mp3, wav, m4a, ogg, flac, aac | `input_audio` (Gemini, Bedrock: `file` with `format`) |
| 동영상 | `video` | mp4, mov, webm, mpeg, mpg, avi | `video_url` (Gemini, Bedrock: `file` with `format`) |
| 문서 | `document` | pdf, doc(x), ppt(x), xls(x), hwp(x), rtf | `file` with a base64 data URI |
| 문서 (text) | `document` | txt, md, markdown, csv, tsv, html, htm, json, xml | a **text** block, not base64 |

Text documents skip base64 because many providers refuse text as a base64 document (Anthropic takes only PDF) while
every model reads text. Any other extension stays a path.

LiteLLM translates these OpenAI-shaped blocks for each provider. What a model accepts is the provider's business; there
is no capability check, because LiteLLM's table does not cover private gateways and rarely states audio or video.

## 2. @mentioned files

1. `expand_mentions()` still appends the `[@참조]` text block with paths. The message text never carries content.
2. `MentionReport.attachment_paths` goes to the engine next to the skill designations — from a new turn and from an
   interjection. It is fixed in `turns.config.attachments`, so a resumed turn keeps it.
3. `SpeechRunner.speak` asks `attachments_for(agent, …)` for what this agent receives, re-checking each path with
   `safe_workspace_path()`.
4. `_call_agent_once` applies the limits (`fit_attachments`) and puts the blocks on the **goal message**
   (`with_attachments`), the one both context trims keep and whose position stays stable for the prompt cache.

Every speech of that turn carries them: plan, debate, graph node, sub-debate, synthesis. Calls that are not speeches
(speaker selection, ledger, summary) and peer consultations do not. **The next turn does not** — mention the file again.

The sender sees a toast for each file that goes to someone, and a warning for one over the limit.

| Limit | Value | When exceeded |
| :--- | :--- | :--- |
| One file | 20 MB (`MAX_ATTACHMENT_BYTES`) | Replaced by a note telling the model it did not get the content |
| One speech | 50 MB (`MAX_SPEECH_ATTACHMENT_BYTES`) | Same; files mentioned first stay |
| Context share | half of the budget (`ATTACHMENT_CONTEXT_SHARE`) | Same |

## 3. Files stay files until the request

Messages hold an app-internal block (`mado_attachment`: path, name, kind, MIME, size). `materialize_attachments()` reads
the files off the event loop right before `litellm.acompletion` and the content is dropped with the request. So:

* speech drafts ([ADR-025](../../lectures/05-adr/ADR-025-resume-speeches-by-tool-step.md)) stay small, and a resumed
  speech reads the file again from the same block;
* `estimate_tokens()` strips the blocks and adds a size-based estimate (`attachment_tokens`) — the tokenizer raises on
  blocks it does not know;
* `merge_consecutive_roles()` and `_append_budget_notice()` join text onto a message whose content is a block list;
  skipping them would bring back the consecutive-user 400 for exactly the agents with a box checked.

A file deleted before the request becomes a note, not a failure.

## 4. Rejected attachments

When a request with attachments is refused (`classify(exc) == REJECTED`), the tool loop replaces every attachment with a
"did not receive" note, tells the reader with a warning at the top of the speech, and asks the same model again
(`_drop_rejected_attachments`). A mismatch between a checkbox and a model must not cost the speech, and once the
conversation started the setting is frozen so the person cannot fix it on the spot. A rejection with no attachments
behaves as before.

## 5. Files returned by tools

`MCPClientConnection._extract_result()` separates text from `ImageContent`, `AudioContent` and blob resources, so base64
never reaches the tool text, the feed or the database. The result is a `ToolOutput`: a `str` subclass carrying `.media`,
which keeps the `(text, status)` contract everywhere else. `ToolLoopMixin._take_tool_media()` then decides per agent:

| The agent… | The file | The tool text says |
| :--- | :--- | :--- |
| receives that format | is stored in `<workspace>/.mado/tool_media/` and attached after the round | it is attached next |
| does not | is dropped | what came back, and not to speak as if it saw it |
| any, format unknown (SVG, ZIP…) | is dropped | what came back, and that it cannot be delivered |

Attachments go in a `user` message after **all** tool results of the round: OpenAI-compatible endpoints take only text
in `role=tool`, and nothing may sit between a tool call and its results. Stored files older than 7 days are deleted when
a new one is stored.

### Sandbox charts

The sandbox (v0.9.0 or later — it is a separate checkout, see `setup_mcp.py`) returns new raster images as `ImageContent`
too when `SANDBOX_INLINE_IMAGES` is on (at most 4, 5 MB each). An older sandbox ignores the setting and returns only its
Markdown links. The default is off, since a host that flattens results to text would pour base64 into the model. MADO turns it
on for the servers it launches (`HOST_SERVER_ENV`); a server's `env` block can override it.

## 6. Lifecycle

`allowed_modalities` follows the same rule as `allowed_mcp_servers`: saved to `conf.json`, applied to conversations not
yet started, frozen into `config_snapshot` at the first turn. See [Roster Editing](roster-editing.md).
