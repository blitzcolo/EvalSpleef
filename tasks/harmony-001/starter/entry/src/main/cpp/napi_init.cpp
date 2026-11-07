// napi_init.cpp — NAPI 绑定层（当前只注册了一个占位函数，需要你补全 5 个图像接口）。
#include <cstdint>
#include <cstring>
#include <memory>

#include "napi/native_api.h"
#include "hilog/log.h"
#include "image_ops.h"

namespace {

constexpr int32_t kArgCount = 3;

napi_value MakeBuffer(napi_env env, const uint8_t* data, size_t length) {
  void* raw = nullptr;
  napi_value buffer = nullptr;
  napi_create_arraybuffer(env, length, &raw, &buffer);
  if (raw != nullptr && data != nullptr) {
    std::memcpy(raw, data, length);
  }
  return buffer;
}

// TODO: 实现 grayscale / adjustBrightness / adjustContrast / boxBlur3 / rotate90 五个绑定函数。
// 要求：
//   1) 入参 (ArrayBuffer, width, height[, 参数])；宽度/高度必须 > 0 且与缓冲区长度一致；
//   2) 调用 image_ops.h 的内核函数；
//   3) 返回新的 ArrayBuffer；参数非法时抛 napi_throw_type_error。

napi_value NopPlaceholder(napi_env env, napi_callback_info info) {
  return MakeBuffer(env, nullptr, 0);
}

napi_value Init(napi_env env, napi_value exports) {
  napi_property_descriptor desc[] = {
      {"nop", nullptr, NopPlaceholder, nullptr, nullptr, nullptr, napi_default, nullptr},
  };
  napi_define_properties(env, exports, sizeof(desc) / sizeof(desc[0]), desc);
  return exports;
}

}  // namespace

static napi_module imageOpsModule = {
    .nm_version = 1,
    .nm_flags = 0,
    .nm_filename = nullptr,
    .nm_register_func = Init,
    .nm_modname = "imageops",
    .nm_priv = nullptr,
    .reserved = {0},
};

extern "C" __attribute__((constructor)) void RegisterImageOpsModule(void) {
  napi_module_register(&imageOpsModule);
}
