#!/usr/bin/env python3
"""Apply V8 10.2.154.26 patches for bytecode disassembly."""

import re
import sys
import os

def patch_file(filepath, patches, description=""):
    """Apply multiple search-replace patches to a file."""
    with open(filepath, 'r') as f:
        content = f.read()

    original = content
    for search, replace, patch_desc in patches:
        if search in content:
            content = content.replace(search, replace, 1)
            print(f"  [OK] {patch_desc}")
        else:
            print(f"  [WARN] Pattern not found for: {patch_desc}")
            print(f"  Searching for: {repr(search[:80])}...")
            # Try regex
            return False

    if content != original:
        with open(filepath, 'w') as f:
            f.write(content)
        print(f"  Patched {filepath}")
        return True
    return False


def patch_objects_printer(v8_dir):
    """Patch objects-printer.cc to output bytecode disassembly."""
    filepath = os.path.join(v8_dir, 'src/diagnostics/objects-printer.cc')
    with open(filepath, 'r') as f:
        content = f.read()

    # 1. Remove PrintSourceCode(os) call
    content = content.replace('  PrintSourceCode(os);\n', '')
    print("  [OK] Removed PrintSourceCode")

    # 2. Add BytecodeArray disassembly at end of SharedFunctionInfoPrint
    # Find the closing of SharedFunctionInfoPrint - look for the pattern:
    # os << "\n";
    # }  (closing brace of SharedFunctionInfoPrint)
    # void JSGlobalProxy::
    marker = 'void JSGlobalProxy::JSGlobalProxyPrint'
    idx = content.find(marker)
    if idx < 0:
        print("  [FAIL] Could not find JSGlobalProxyPrint marker")
        return False

    # Find the closing brace before this marker
    brace_idx = content.rfind('}', 0, idx)
    # Find the os << "\n"; before the closing brace
    newline_idx = content.rfind('os << "\\n";', 0, brace_idx)
    if newline_idx < 0:
        print("  [FAIL] Could not find os << newline before JSGlobalProxyPrint")
        return False

    # Insert after os << "\n";
    insert_point = content.find('\n', newline_idx) + 1
    insert_code = '''  os << "\\nStart BytecodeArray\\n";
  this->GetActiveBytecodeArray().Disassemble(os);
  os << "\\nEnd BytecodeArray\\n";
  os << std::flush;
'''
    content = content[:insert_point] + insert_code + content[insert_point:]
    print("  [OK] Added BytecodeArray disassembly")

    with open(filepath, 'w') as f:
        f.write(content)
    return True


def patch_objects_cc(v8_dir):
    """Patch objects.cc - minimal changes, no recursive SFI printing."""
    filepath = os.path.join(v8_dir, 'src/objects/objects.cc')
    with open(filepath, 'r') as f:
        content = f.read()

    # Only keep the ASM_WASM_DATA_TYPE, FixedArray, ObjectBoilerplate, FixedDoubleArray patches
    # NO recursive SharedFunctionInfo printing here (moved to code-serializer.cc)

    # 1. Add ASM_WASM_DATA_TYPE check before switch statement
    switch_marker = '  switch (map(cage_base).instance_type()) {'
    if switch_marker in content:
        insert = '''  // Print array literal members
  if (map(cage_base).instance_type() == ASM_WASM_DATA_TYPE) {
    os << "<ArrayBoilerplateDescription> ";
    ArrayBoilerplateDescription::cast(*this)
        .constant_elements()
        .HeapObjectShortPrint(os);
    return;
  }

'''
        content = content.replace(switch_marker, insert + switch_marker, 1)
        print("  [OK] Added ASM_WASM_DATA_TYPE check")

    # 2. Add FixedArray printing
    fa_marker = '      os << "<FixedArray[" << FixedArray::cast(*this).length() << "]>";'
    if fa_marker in content:
        replacement = fa_marker + '''
      os << "\\nStart FixedArray\\n";
      FixedArray::cast(*this).FixedArrayPrint(os);
      os << "\\nEnd FixedArray\\n";'''
        content = content.replace(fa_marker, replacement, 1)
        print("  [OK] Added FixedArray printing")

    # 3. Add ObjectBoilerplateDescription printing
    obd_pattern = '      os << "<ObjectBoilerplateDescription[" << FixedArray::cast(*this).length()\n         << "]>";'
    if obd_pattern in content:
        replacement = obd_pattern + '''
      os << "\\nStart ObjectBoilerplateDescription\\n";
      ObjectBoilerplateDescription::cast(*this)
          .ObjectBoilerplateDescriptionPrint(os);
      os << "\\nEnd ObjectBoilerplateDescription\\n";'''
        content = content.replace(obd_pattern, replacement, 1)
        print("  [OK] Added ObjectBoilerplateDescription printing")

    # 4. Add FixedDoubleArray printing
    fda_pattern = '      os << "<FixedDoubleArray[" << FixedDoubleArray::cast(*this).length()\n         << "]>";'
    if fda_pattern in content:
        replacement = fda_pattern + '''
      os << "\\nStart FixedDoubleArray\\n";
      FixedDoubleArray::cast(*this).FixedDoubleArrayPrint(os);
      os << "\\nEnd FixedDoubleArray\\n";'''
        content = content.replace(fda_pattern, replacement, 1)
        print("  [OK] Added FixedDoubleArray printing")

    # NOTE: No recursive SharedFunctionInfo printing here.
    # Recursive traversal is handled in code-serializer.cc with a visited set.

    with open(filepath, 'w') as f:
        f.write(content)
    return True


def patch_string_cc(v8_dir):
    """Remove string truncation in string.cc."""
    filepath = os.path.join(v8_dir, 'src/objects/string.cc')
    with open(filepath, 'r') as f:
        content = f.read()

    # Remove the truncation block
    truncation = '''  if (len > kMaxShortPrintLength) {
    accumulator->Add("...<truncated>>");
    accumulator->Add(SuffixForDebugPrint());
    accumulator->Put('>');
    return;
  }'''

    if truncation in content:
        content = content.replace(truncation, '  // Truncation removed for full string output')
        print("  [OK] Removed string truncation")
    else:
        # Try with different whitespace
        pattern = r'  if \(len > kMaxShortPrintLength\) \{[^}]+\}'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            content = content[:match.start()] + '  // Truncation removed for full string output' + content[match.end():]
            print("  [OK] Removed string truncation (regex)")
        else:
            print("  [WARN] Could not find truncation block")

    with open(filepath, 'w') as f:
        f.write(content)
    return True


def patch_code_serializer(v8_dir):
    """Patch code-serializer.cc to print SFI and bypass sanity checks."""
    filepath = os.path.join(v8_dir, 'src/snapshot/code-serializer.cc')
    with open(filepath, 'r') as f:
        content = f.read()

    # 1. Add necessary includes
    if '#include <iostream>' not in content:
        last_include = content.rfind('#include ')
        end_of_include_line = content.find('\n', last_include) + 1
        includes = '#include <iostream>\n#include <set>\n#include "src/objects/shared-function-info-inl.h"\n'
        content = content[:end_of_include_line] + includes + content[end_of_include_line:]
        print("  [OK] Added includes (iostream, set, shared-function-info-inl.h)")

    # 2. Replace SanityCheck to always return success
    sanity_pattern = r'(SerializedCodeSanityCheckResult SerializedCodeData::SanityCheck\(\s*uint32_t expected_source_hash\) const \{)(.*?)(return SanityCheckJustSource\(expected_source_hash\);)'
    match = re.search(sanity_pattern, content, re.DOTALL)
    if match:
        start = match.start()
        end = match.end()
        replacement = match.group(1) + '\n  return SerializedCodeSanityCheckResult::kSuccess;'
        # Find the closing brace
        brace_end = content.find('}', end)
        content = content[:start] + replacement + '\n' + content[brace_end:]
        print("  [OK] Bypassed SanityCheck")
    else:
        print("  [WARN] Could not find SanityCheck pattern, trying alternative")
        # Alternative: just replace the function body
        alt_marker = 'SerializedCodeSanityCheckResult SerializedCodeData::SanityCheck('
        idx = content.find(alt_marker)
        if idx >= 0:
            # Find opening brace
            brace_start = content.find('{', idx)
            # Find matching closing brace (simple heuristic - find next } after the function)
            depth = 1
            pos = brace_start + 1
            while depth > 0 and pos < len(content):
                if content[pos] == '{': depth += 1
                elif content[pos] == '}': depth -= 1
                pos += 1
            content = content[:brace_start+1] + '\n  return SerializedCodeSanityCheckResult::kSuccess;\n' + content[pos-1:]
            print("  [OK] Bypassed SanityCheck (alt)")

    # 3. Add recursive SFI traversal function and call it after deserialization
    # First, add the helper function before the Deserialize function
    deserialize_sig = 'MaybeHandle<SharedFunctionInfo> CodeSerializer::Deserialize('
    idx = content.find(deserialize_sig)
    if idx >= 0:
        helper_func = '''
// Recursive SFI printer with visited set to avoid cycles
static void PrintAllSFIs(SharedFunctionInfo sfi, std::set<Address>& visited) {
  Address addr = sfi.ptr();
  if (visited.count(addr)) return;
  visited.insert(addr);

  std::cout << "\\nStart SharedFunctionInfo\\n";
  sfi.SharedFunctionInfoPrint(std::cout);
  std::cout << "\\nEnd SharedFunctionInfo\\n";
  std::cout << std::flush;

  if (!sfi.HasBytecodeArray()) return;
  BytecodeArray bytecodes = sfi.GetActiveBytecodeArray();
  if (!bytecodes.HasConstantPool()) return;
  FixedArray constants = bytecodes.constant_pool();
  for (int i = 0; i < constants.length(); i++) {
    Object obj = constants.get(i);
    if (obj.IsSharedFunctionInfo()) {
      PrintAllSFIs(SharedFunctionInfo::cast(obj), visited);
    }
  }
}

'''
        content = content[:idx] + helper_func + content[idx:]
        print("  [OK] Added PrintAllSFIs helper function")

    # Now add the call after deserialization
    finalize_marker = 'FinalizeDeserialization(isolate, result, timer);'
    idx = content.find(finalize_marker)
    if idx >= 0:
        insert_code = '''{
    std::set<Address> visited;
    PrintAllSFIs(*result, visited);
    std::cout << "\\n// Total functions printed: " << visited.size() << "\\n";
  }

  '''
        content = content[:idx] + insert_code + content[idx:]
        print("  [OK] Added recursive SFI traversal call")
    else:
        fallback_marker = 'return scope.CloseAndEscape(result);'
        idx = content.find(fallback_marker)
        if idx >= 0:
            insert_code = '''{
    std::set<Address> visited;
    PrintAllSFIs(*result, visited);
    std::cout << "\\n// Total functions printed: " << visited.size() << "\\n";
  }

  '''
            content = content[:idx] + insert_code + content[idx:]
            print("  [OK] Added recursive SFI traversal call (fallback)")
        else:
            print("  [WARN] Could not find insertion point for SFI traversal")

    with open(filepath, 'w') as f:
        f.write(content)
    return True


def patch_deserializer(v8_dir):
    """Remove magic number check in deserializer.cc."""
    filepath = os.path.join(v8_dir, 'src/snapshot/deserializer.cc')
    with open(filepath, 'r') as f:
        content = f.read()

    check = 'CHECK_EQ(magic_number_, SerializedData::kMagicNumber);'
    if check in content:
        content = content.replace(check, '// ' + check)
        print("  [OK] Disabled magic number check")

    with open(filepath, 'w') as f:
        f.write(content)
    return True


def main():
    v8_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/v8')

    print("Patching V8 source for bytecode disassembly...")

    print("\n1. Patching objects-printer.cc...")
    patch_objects_printer(v8_dir)

    print("\n2. Patching objects.cc...")
    patch_objects_cc(v8_dir)

    print("\n3. Patching string.cc...")
    patch_string_cc(v8_dir)

    print("\n4. Patching code-serializer.cc...")
    patch_code_serializer(v8_dir)

    print("\n5. Patching deserializer.cc...")
    patch_deserializer(v8_dir)

    print("\nAll patches applied!")


if __name__ == '__main__':
    main()
