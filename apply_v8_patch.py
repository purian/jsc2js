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
    """Patch objects.cc to print detailed object info."""
    filepath = os.path.join(v8_dir, 'src/objects/objects.cc')
    with open(filepath, 'r') as f:
        content = f.read()

    # 0. Add depth counter at the top of HeapObjectShortPrint function
    short_print_sig = 'void HeapObject::HeapObjectShortPrint(std::ostream& os) {'
    if short_print_sig in content:
        depth_code = short_print_sig + '''
  static thread_local int sfi_print_depth = 0;'''
        content = content.replace(short_print_sig, depth_code, 1)
        print("  [OK] Added depth counter to HeapObjectShortPrint")

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

    # 5. Add nested SharedFunctionInfo printing
    # The code has an if/else: if name exists, prints with name; else without.
    # We need to add recursive printing AFTER both branches, before break;
    # Look for the SHARED_FUNCTION_INFO_TYPE case block
    sfi_case = 'case SHARED_FUNCTION_INFO_TYPE:'
    if sfi_case in content:
        # Find the break; that ends this case
        case_idx = content.find(sfi_case)
        # Find the closing brace of the if-else block after the case
        # Pattern: ...} else { ... os << "<SharedFunctionInfo>"; } break;
        # We need to insert before 'break;' in this case
        break_search_start = case_idx
        # Find the 'break;' that belongs to this case
        # It's after the closing brace of the SharedFunctionInfo block
        break_idx = content.find('      break;', break_search_start)
        if break_idx > 0:
            insert_code = '''      if (sfi_print_depth < 200) {
        sfi_print_depth++;
        os << "\\nStart SharedFunctionInfo\\n";
        shared.SharedFunctionInfoPrint(os);
        os << "\\nEnd SharedFunctionInfo\\n";
        sfi_print_depth--;
      }
'''
            content = content[:break_idx] + insert_code + content[break_idx:]
            print("  [OK] Added nested SharedFunctionInfo printing with depth limit")
        else:
            print("  [WARN] Could not find break; for SHARED_FUNCTION_INFO_TYPE case")

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

    # 1. Add iostream include
    if '#include <iostream>' not in content:
        # Add after the last #include
        last_include = content.rfind('#include ')
        end_of_include_line = content.find('\n', last_include) + 1
        content = content[:end_of_include_line] + '#include <iostream>\n' + content[end_of_include_line:]
        print("  [OK] Added #include <iostream>")

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

    # 3. Add SFI printing after successful deserialization
    # Use FinalizeDeserialization as anchor - it's unique and comes right after result is ready
    finalize_marker = 'FinalizeDeserialization(isolate, result, timer);'
    idx = content.find(finalize_marker)
    if idx >= 0:
        insert_code = '''std::cout << "\\nStart SharedFunctionInfo\\n";
  result->SharedFunctionInfoPrint(std::cout);
  std::cout << "\\nEnd SharedFunctionInfo\\n";
  std::cout << std::flush;

  '''
        content = content[:idx] + insert_code + content[idx:]
        print("  [OK] Added SFI print before FinalizeDeserialization")
    else:
        # Fallback: look for "return scope.CloseAndEscape(result);"
        fallback_marker = 'return scope.CloseAndEscape(result);'
        idx = content.find(fallback_marker)
        if idx >= 0:
            insert_code = '''std::cout << "\\nStart SharedFunctionInfo\\n";
  result->SharedFunctionInfoPrint(std::cout);
  std::cout << "\\nEnd SharedFunctionInfo\\n";
  std::cout << std::flush;

  '''
            content = content[:idx] + insert_code + content[idx:]
            print("  [OK] Added SFI print before CloseAndEscape (fallback)")
        else:
            print("  [WARN] Could not find insertion point for SFI print")

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
