// v8dasm_internal.cpp - V8 bytecode disassembler using internal APIs
// Performs iterative SFI traversal with per-entry crash recovery
// Build with: clang++ v8dasm_internal.cpp -g -std=c++17 -I. -Iinclude
//   -Lout.gn/x64.release/obj -lv8_monolith -lv8_libbase -lv8_libplatform
//   -ldl -pthread -o v8dasm_internal

#include <fstream>
#include <iostream>
#include <string>
#include <vector>
#include <set>
#include <csignal>
#include <csetjmp>

#include "libplatform/libplatform.h"
#include "v8.h"

// V8 internal headers for direct SFI access
#include "src/api/api.h"
#include "src/api/api-inl.h"
#include "src/objects/shared-function-info.h"
#include "src/objects/shared-function-info-inl.h"
#include "src/objects/bytecode-array.h"
#include "src/objects/bytecode-array-inl.h"
#include "src/objects/fixed-array.h"
#include "src/objects/fixed-array-inl.h"

using namespace v8;

// Crash recovery
static thread_local jmp_buf crash_buf;
static thread_local volatile bool crash_active = false;

static void crash_handler(int sig) {
  if (crash_active) {
    crash_active = false;
    longjmp(crash_buf, sig);
  }
  signal(sig, SIG_DFL);
  raise(sig);
}

static void PrintAllSFIs(i::SharedFunctionInfo root_sfi) {
  std::vector<i::Address> queue;
  std::set<i::Address> visited;
  queue.push_back(root_sfi.ptr());

  struct sigaction sa, old_sa, old_bus;
  sa.sa_handler = crash_handler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;

  int count = 0;
  int crashes = 0;

  while (!queue.empty()) {
    i::Address addr = queue.back();
    queue.pop_back();

    if (visited.count(addr)) continue;
    visited.insert(addr);
    count++;

    sigaction(SIGSEGV, &sa, &old_sa);
    sigaction(SIGBUS, &sa, &old_bus);

    i::SharedFunctionInfo sfi = i::SharedFunctionInfo::cast(i::Object(addr));

    std::cerr << "[" << count << "] SFI 0x" << std::hex << addr << std::dec
              << " (queue: " << queue.size() << ")\n";

    std::cout << "\nStart SharedFunctionInfo\n";

    // 1. Print SFI metadata
    crash_active = true;
    if (setjmp(crash_buf) == 0) {
      sfi.SharedFunctionInfoPrint(std::cout);
      crash_active = false;
    } else {
      crashes++;
      std::cout << "\n// [CRASH RECOVERED during SharedFunctionInfoPrint]\n";
      std::cerr << "  CRASH in SharedFunctionInfoPrint\n";
    }

    // 2. Check HasBytecodeArray
    volatile bool has_bc = false;
    crash_active = true;
    if (setjmp(crash_buf) == 0) {
      has_bc = sfi.HasBytecodeArray();
      crash_active = false;
    } else {
      crashes++;
      std::cerr << "  CRASH checking HasBytecodeArray\n";
    }

    // 3. Print BytecodeArray
    if (has_bc) {
      crash_active = true;
      if (setjmp(crash_buf) == 0) {
        i::BytecodeArray bc = sfi.GetActiveBytecodeArray();
        bc.Disassemble(std::cout);
        crash_active = false;
      } else {
        crashes++;
        std::cout << "\n// [CRASH RECOVERED during Disassemble]\n";
        std::cerr << "  CRASH in Disassemble\n";
      }
    }

    std::cout << "\nEnd SharedFunctionInfo\n" << std::flush;

    // 4. Collect child SFIs from constant pool (per-entry crash recovery)
    if (has_bc) {
      volatile int pool_len = 0;
      crash_active = true;
      if (setjmp(crash_buf) == 0) {
        i::BytecodeArray bc = sfi.GetActiveBytecodeArray();
        i::FixedArray cp = bc.constant_pool();
        pool_len = cp.length();
        crash_active = false;
      } else {
        crashes++;
        std::cerr << "  CRASH getting pool length\n";
      }

      for (volatile int i = 0; i < pool_len; i++) {
        crash_active = true;
        if (setjmp(crash_buf) == 0) {
          i::BytecodeArray bc = sfi.GetActiveBytecodeArray();
          i::FixedArray cp = bc.constant_pool();
          i::Object obj = cp.get(i);
          crash_active = false;
          if (obj.IsSharedFunctionInfo()) {
            i::Address child = i::SharedFunctionInfo::cast(obj).ptr();
            if (!visited.count(child)) {
              queue.push_back(child);
            }
          }
        } else {
          crashes++;
          // Skip this entry, continue to next
        }
      }
    }

    sigaction(SIGSEGV, &old_sa, nullptr);
    sigaction(SIGBUS, &old_bus, nullptr);
  }

  std::cout << "\n// Total: " << count << " functions\n";
  std::cout << "// Crashes recovered: " << crashes << "\n";
  std::cerr << "Done: " << count << " functions, " << crashes << " crashes\n";
}

static void readAllBytes(const std::string& file, std::vector<char>& buffer) {
  std::ifstream infile(file, std::ios::binary);
  infile.seekg(0, infile.end);
  size_t length = infile.tellg();
  infile.seekg(0, infile.beg);
  if (length > 0) {
    buffer.resize(length);
    infile.read(&buffer[0], length);
  }
}

int main(int argc, char* argv[]) {
  if (argc < 2) {
    std::cerr << "Usage: " << argv[0] << " <file.jsc>\n";
    return 1;
  }

  V8::SetFlagsFromString("--no-lazy --no-flush-bytecode");

  V8::InitializeICU();
  std::unique_ptr<Platform> platform = platform::NewDefaultPlatform();
  V8::InitializePlatform(platform.get());
  V8::Initialize();

  Isolate::CreateParams params;
  params.array_buffer_allocator = ArrayBuffer::Allocator::NewDefaultAllocator();

  Isolate* isolate = Isolate::New(params);
  {
    Isolate::Scope isolate_scope(isolate);
    HandleScope scope(isolate);

    std::vector<char> data;
    readAllBytes(argv[1], data);

    ScriptCompiler::CachedData* cached =
        new ScriptCompiler::CachedData((uint8_t*)data.data(), data.size());

    ScriptOrigin origin(isolate, String::NewFromUtf8Literal(isolate, "code.jsc"));
    ScriptCompiler::Source source(
        String::NewFromUtf8Literal(isolate, "\"\\xE0\\xB2\\xA0_\\xE0\\xB2\\xA0\""),
        origin, cached);

    MaybeLocal<UnboundScript> maybe = ScriptCompiler::CompileUnboundScript(
        isolate, &source, ScriptCompiler::kConsumeCodeCache);

    if (!maybe.IsEmpty()) {
      Local<UnboundScript> script = maybe.ToLocalChecked();
      // Extract internal SharedFunctionInfo from UnboundScript
      i::SharedFunctionInfo sfi = *Utils::OpenHandle(*script);
      std::cerr << "Loaded JSC, starting traversal...\n";
      PrintAllSFIs(sfi);
    } else {
      std::cerr << "Failed to compile from cache\n";
      if (cached->rejected) {
        std::cerr << "Cache was REJECTED\n";
      }
    }
  }

  isolate->Dispose();
  V8::Dispose();
  V8::DisposePlatform();
  return 0;
}
