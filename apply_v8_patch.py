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

    # Do NOT add BytecodeArray::Disassemble here - it crashes due to HeapObjectShortPrint
    # BytecodeArray printing is handled separately in code-serializer.cc with crash recovery
    print("  [SKIP] BytecodeArray disassembly handled in code-serializer.cc")

    with open(filepath, 'w') as f:
        f.write(content)
    return True


def patch_objects_cc(v8_dir):
    """No changes to objects.cc - all recursive handling is in code-serializer.cc."""
    print("  [SKIP] No objects.cc changes needed (recursive printing handled in code-serializer.cc)")
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
        includes = '#include <iostream>\n#include <vector>\n#include <set>\n#include <csignal>\n#include <csetjmp>\n#include "src/objects/shared-function-info-inl.h"\n'
        content = content[:end_of_include_line] + includes + content[end_of_include_line:]
        print("  [OK] Added includes")

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
// Crash recovery for SIGSEGV/SIGBUS during disassembly
static thread_local jmp_buf crash_recovery_buf;
static thread_local volatile bool crash_handler_active = false;

static void crash_signal_handler(int sig) {
  if (crash_handler_active) {
    crash_handler_active = false;
    longjmp(crash_recovery_buf, sig);
  }
  // Re-raise if not ours
  signal(sig, SIG_DFL);
  raise(sig);
}

// Iterative SFI printer with work queue and per-entry crash recovery
static void PrintAllSFIs(SharedFunctionInfo root_sfi, std::set<Address>& visited) {
  std::vector<Address> work_queue;
  work_queue.push_back(root_sfi.ptr());

  struct sigaction sa, old_sa, old_bus;
  sa.sa_handler = crash_signal_handler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;

  int sfi_count = 0;
  int crash_count = 0;

  while (!work_queue.empty()) {
    Address current_addr = work_queue.back();
    work_queue.pop_back();

    if (visited.count(current_addr)) continue;
    visited.insert(current_addr);
    sfi_count++;

    sigaction(SIGSEGV, &sa, &old_sa);
    sigaction(SIGBUS, &sa, &old_bus);

    SharedFunctionInfo sfi = SharedFunctionInfo::cast(Object(current_addr));

    std::cerr << "[SFI " << sfi_count << "] Processing 0x"
              << std::hex << current_addr << std::dec
              << " (queue: " << work_queue.size() << ")\\n";

    std::cout << "\\nStart SharedFunctionInfo\\n";

    // 1. Print SFI metadata with crash recovery
    crash_handler_active = true;
    if (setjmp(crash_recovery_buf) == 0) {
      sfi.SharedFunctionInfoPrint(std::cout);
      crash_handler_active = false;
    } else {
      crash_count++;
      std::cout << "\\n// [CRASH RECOVERED during SharedFunctionInfoPrint]\\n";
    }

    // 2. Check if has bytecode array (safely)
    volatile bool has_bytecodes = false;
    crash_handler_active = true;
    if (setjmp(crash_recovery_buf) == 0) {
      has_bytecodes = sfi.HasBytecodeArray();
      crash_handler_active = false;
    } else {
      crash_count++;
      std::cerr << "  CRASH checking HasBytecodeArray\\n";
    }

    // 3. Print BytecodeArray with crash recovery
    if (has_bytecodes) {
      crash_handler_active = true;
      if (setjmp(crash_recovery_buf) == 0) {
        BytecodeArray bytecodes = sfi.GetActiveBytecodeArray();
        bytecodes.Disassemble(std::cout);
        crash_handler_active = false;
      } else {
        crash_count++;
        std::cout << "\\n// [CRASH RECOVERED during BytecodeArray::Disassemble]\\n";
        std::cerr << "  CRASH during Disassemble\\n";
      }
    }

    std::cout << "\\nEnd SharedFunctionInfo\\n" << std::flush;

    // 4. Collect child SFIs from constant pool with PER-ENTRY crash recovery
    if (has_bytecodes) {
      // Get pool length safely
      volatile int pool_length = 0;
      crash_handler_active = true;
      if (setjmp(crash_recovery_buf) == 0) {
        BytecodeArray bc = sfi.GetActiveBytecodeArray();
        FixedArray cp = bc.constant_pool();
        pool_length = cp.length();
        crash_handler_active = false;
      } else {
        crash_count++;
        std::cerr << "  CRASH getting constant pool length\\n";
      }

      // Iterate each constant pool entry INDIVIDUALLY
      for (volatile int i = 0; i < pool_length; i++) {
        crash_handler_active = true;
        if (setjmp(crash_recovery_buf) == 0) {
          BytecodeArray bc = sfi.GetActiveBytecodeArray();
          FixedArray cp = bc.constant_pool();
          Object obj = cp.get(i);
          crash_handler_active = false;
          if (obj.IsSharedFunctionInfo()) {
            Address child_addr = SharedFunctionInfo::cast(obj).ptr();
            if (!visited.count(child_addr)) {
              work_queue.push_back(child_addr);
            }
          }
        } else {
          // Skip this entry, continue to next - DO NOT BREAK
          crash_count++;
        }
      }
    }

    sigaction(SIGSEGV, &old_sa, nullptr);
    sigaction(SIGBUS, &old_bus, nullptr);
  }

  std::cout << "\\n// Total functions printed: " << sfi_count << "\\n";
  std::cout << "// Total crashes recovered: " << crash_count << "\\n";
  std::cerr << "Done: " << sfi_count << " SFIs printed, "
            << crash_count << " crashes recovered\\n";
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
  }

  '''
        content = content[:idx] + insert_code + content[idx:]
        print("  [OK] Added iterative SFI traversal call")
    else:
        fallback_marker = 'return scope.CloseAndEscape(result);'
        idx = content.find(fallback_marker)
        if idx >= 0:
            insert_code = '''{
    std::set<Address> visited;
    PrintAllSFIs(*result, visited);
  }

  '''
            content = content[:idx] + insert_code + content[idx:]
            print("  [OK] Added iterative SFI traversal call (fallback)")
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
