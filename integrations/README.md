# Proteus Integration Scripts

## auto_compress.py
Pre-compresses files for use with Proteus::
    python auto_compress.py <path>
    
Creates a `.compressed` companion file. The original is left untouched.

## Hermes Hook Integration
To automatically compress large tool outputs in Hermes:
    from proteus import compress_tool_output
    compressed, stats = compress_tool_output(large_tool_result)
