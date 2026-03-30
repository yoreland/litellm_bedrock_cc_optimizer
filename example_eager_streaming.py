#!/usr/bin/env python3
"""
Example demonstrating eager_input_streaming optimization.

Before optimization:
{
  "tools": [{"name": "write_file", "input_schema": {...}}]
}

After optimization:
{
  "tools": [{"name": "write_file", "eager_input_streaming": true, "input_schema": {...}}]
}

Result: Tool parameters stream character-by-character without buffering,
reducing latency for large content like code blocks or long text.
"""

from bedrock_optimizer import inject_eager_input_streaming

# Example 1: Simple tool definition
data = {
    "messages": [{"role": "user", "content": "Write a long poem to poem.txt"}],
    "tools": [
        {
            "name": "write_file",
            "description": "Write text to a file",
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string"}
                }
            }
        }
    ]
}

print("🔧 Example 1: Single tool optimization")
print("Before:", data["tools"][0].get("eager_input_streaming"))
added, action = inject_eager_input_streaming(data)
print("After:", data["tools"][0].get("eager_input_streaming"))
print(f"Result: {action}\n")

# Example 2: Multiple tools
data2 = {
    "messages": [{"role": "user", "content": "test"}],
    "tools": [
        {"name": "read_file", "input_schema": {}},
        {"name": "write_file", "input_schema": {}},
        {"name": "execute_code", "input_schema": {}},
    ]
}

print("🔧 Example 2: Multiple tools")
print(f"Tools before: {len([t for t in data2['tools'] if 'eager_input_streaming' in t])} with eager_input_streaming")
added, action = inject_eager_input_streaming(data2)
print(f"Tools after: {len([t for t in data2['tools'] if t.get('eager_input_streaming')])} with eager_input_streaming")
print(f"Result: {action}\n")

# Example 3: Preserve user's explicit config
data3 = {
    "messages": [{"role": "user", "content": "test"}],
    "tools": [
        {"name": "sensitive_op", "eager_input_streaming": False},  # User wants buffering
        {"name": "write_file"},  # Will be optimized
    ]
}

print("🔧 Example 3: Respect user config")
print("Before:")
print(f"  - sensitive_op: {data3['tools'][0].get('eager_input_streaming')}")
print(f"  - write_file: {data3['tools'][1].get('eager_input_streaming')}")
added, action = inject_eager_input_streaming(data3)
print("After:")
print(f"  - sensitive_op: {data3['tools'][0].get('eager_input_streaming')} (preserved)")
print(f"  - write_file: {data3['tools'][1].get('eager_input_streaming')} (injected)")
print(f"Result: {action}\n")

print("✅ All examples completed. The optimizer respects user intent while")
print("   automatically optimizing tools for low-latency streaming.")
