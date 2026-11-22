// Stand-in for the runtime exports of `@earendil-works/pi-coding-agent` that
// the extensions call. Events in the test are built as
// `{ toolName, input, isError }`, so the guards only need the tool name.
export function isToolCallEventType(toolName, event) {
  return event?.toolName === toolName;
}

export function isBashToolResult(event) {
  return event?.toolName === "bash";
}
