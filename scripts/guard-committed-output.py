#!/usr/bin/env python3
"""Prevent agent-level automatic replay of already committed output.

Pi agent-loop stops before executing tool calls on stopReason=error, allowing
a retry of partial unexecuted tool calls without losing work. The guard rejects
committed text/images and any tool calls with an observed result.
"""
import argparse
from pathlib import Path


def replace_exact(source, before, after):
    count = source.count(before)
    if count != 1:
        raise ValueError(f"Unsupported Pi retry implementation: expected one {before[:64]!r}, got {count}")
    return source.replace(before, after)


def guard_source(source):
    source = replace_exact(
        source,
        'return this._isRetryableError(message);',
        'return this._isRetryableError(message) && !this._hasCommittedReplayOutput(message);',
    )
    source = replace_exact(
        source,
        'if (this._isRetryableError(message) && (await this._prepareRetry(message))) {',
        'if (this._isRetryableError(message) && !this._hasCommittedReplayOutput(message) && (await this._prepareRetry(message))) {',
    )
    method_marker = '    _isRetryableError(message) {\n'
    method = '''    /** OMP-style replay safety: preserve visible output and completed tool effects. */
    _hasCommittedReplayOutput(message) {
        const blocks = message.content ?? [];
        if (blocks.some((block) =>
            block?.type === "image" ||
            block?.type === "anthropicServerTool" ||
            (block?.type === "text" && /\\S/.test(block.text ?? ""))))
            return true;
        const toolIds = new Set(blocks.filter((b) => b?.type === "toolCall").map((b) => b.id));
        if (!toolIds.size) return false;
        const history = this.agent.state.messages;
        const index = history.lastIndexOf(message);
        // Without proof which turn was interrupted, never risk a replay.
        if (index < 0) return true;
        for (let i = index + 1; i < history.length; i++) {
            const later = history[i];
            if (later.role === "assistant" || later.role === "user") break;
            if (later.role === "toolResult" && toolIds.has(later.toolCallId)) return true;
        }
        // Pi's agent loop stops BEFORE executing tools on stopReason=error;
        // emitted but unpaired toolCall blocks are safe to retry.
        return false;
    }
'''
    return replace_exact(source, method_marker, method + method_marker)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True, type=Path)
    args = parser.parse_args()
    filename = args.package / 'dist/core/agent-session.js'
    source = filename.read_text()
    filename.write_text(guard_source(source))
    print('Protected Pi agent retry from replaying committed content')


if __name__ == '__main__':
    main()
