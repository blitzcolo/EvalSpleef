// main.cpp — Vulkan 阴影场景骨架。
//
// 现状：实例/设备/交换链/主通道管线/命令录制的骨架已搭好，但以下四处是空的：
//   1) 验证层与 debug messenger（平台要求零 error）
//   2) 深度缓冲与阴影贴图资源
//   3) 阴影通道（单 pass 直接采样 或 两 pass 先渲染深度，两种都可）
//   4) 截图导出 out/scene.ppm（P6）
// 你可以自由重构本文件，只要求：引用 shadow_math.h 的矩阵/PCF 结果，且在验证层下零 error。
#include <vulkan/vulkan.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "shadow_math.h"

namespace {

constexpr int kWidth = 800;
constexpr int kHeight = 600;

struct Frame {
  VkImage image = VK_NULL_HANDLE;
  VkImageView view = VK_NULL_HANDLE;
};

struct App {
  VkInstance instance = VK_NULL_HANDLE;
  VkDebugUtilsMessengerEXT debug_messenger = VK_NULL_HANDLE;
  VkPhysicalDevice physical = VK_NULL_HANDLE;
  VkDevice device = VK_NULL_HANDLE;
  VkQueue graphics_queue = VK_NULL_HANDLE;
  VkSwapchainKHR swapchain = VK_NULL_HANDLE;
  std::vector<Frame> frames;
  VkImage depth_image = VK_NULL_HANDLE;
  VkImageView depth_view = VK_NULL_HANDLE;
  VkImage shadow_image = VK_NULL_HANDLE;
  VkImageView shadow_view = VK_NULL_HANDLE;
  VkRenderPass render_pass = VK_NULL_HANDLE;
  VkPipeline main_pipeline = VK_NULL_HANDLE;
  VkPipeline shadow_pipeline = VK_NULL_HANDLE;
  VkDescriptorSetLayout descriptor_layout = VK_NULL_HANDLE;
  VkDescriptorPool descriptor_pool = VK_NULL_HANDLE;
  VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
  VkCommandPool command_pool = VK_NULL_HANDLE;
  VkCommandBuffer command_buffer = VK_NULL_HANDLE;
};

// 平台会调用；返回 0 表示成功。
VkBool32 VKAPI_CALL DebugCallback(VkDebugUtilsMessageSeverityFlagBitsEXT severity,
                                 VkDebugUtilsMessageTypeFlagsEXT /*types*/,
                                 const VkDebugUtilsMessengerCallbackDataEXT* data,
                                 void* /*user*/) {
  if (severity >= VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT) {
    std::fprintf(stderr, "[vk] %s\n", data->pMessage);
  }
  return VK_FALSE;
}

bool CreateInstance(App* app) {
  VkApplicationInfo app_info{VK_STRUCTURE_TYPE_APPLICATION_INFO};
  app_info.apiVersion = VK_API_VERSION_1_3;
  VkInstanceCreateInfo info{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
  info.pApplicationInfo = &app_info;
  // TODO(1): 在 ppEnabledLayerNames 里加入 "VK_LAYER_KHRONOS_validation"，
  //          并在 VK_EXT_debug_utils 可用时创建 debug messenger。
  return vkCreateInstance(&info, nullptr, &app->instance) == VK_SUCCESS;
}

bool PickPhysicalDevice(App* app) {
  uint32_t count = 0;
  vkEnumeratePhysicalDevices(app->instance, &count, nullptr);
  if (count == 0) {
    return false;
  }
  std::vector<VkPhysicalDevice> devices(count);
  vkEnumeratePhysicalDevices(app->instance, &count, devices.data());
  app->physical = devices[0];
  return true;
}

bool CreateDevice(App* app) {
  float priority = 1.0f;
  VkDeviceQueueCreateInfo queue{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
  queue.queueCount = 1;
  queue.pQueuePriorities = &priority;
  VkPhysicalDeviceFeatures features{};
  VkDeviceCreateInfo info{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
  info.queueCreateInfoCount = 1;
  info.pQueueCreateInfos = &queue;
  info.pEnabledFeatures = &features;
  if (vkCreateDevice(app->physical, &info, nullptr, &app->device) != VK_SUCCESS) {
    return false;
  }
  vkGetDeviceQueue(app->device, 0, 0, &app->graphics_queue);
  return true;
}

bool CreateDepthResources(App* app) {
  // TODO(2): 创建 VK_FORMAT_D32_SFLOAT 深度图（VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT），
  //          并把它作为 render_pass 的 depth attachment。
  (void)app;
  return true;
}

bool CreateShadowResources(App* app) {
  // TODO(2): 创建 1024x1024 阴影深度图：
  //   usage = DEPTH_STENCIL_ATTACHMENT | SAMPLED，format = VK_FORMAT_D32_SFLOAT
  //   并创建 sampler（VK_FILTER_LINEAR / CLAMP_TO_BORDER）与 descriptor。
  (void)app;
  return true;
}

bool CreatePipelines(App* app) {
  // TODO(3): 至少建主通道管线；若走两 pass，再加一条阴影通道管线。
  (void)app;
  return true;
}

bool RecordFrame(App* app, uint32_t image_index) {
  // TODO(3): 录制命令：阴影通道（可选）→ 主通道（采样阴影贴图）→ 提交。
  (void)app;
  (void)image_index;
  return true;
}

bool SaveScreenshotPPM(const char* path, const std::vector<unsigned char>& rgba, int width, int height) {
  // TODO(4): 写 P6 PPM：header 为 "P6\n<w> <h>\n255\n"，随后是 RGB 三元组（丢弃 alpha）。
  (void)path;
  (void)rgba;
  (void)width;
  (void)height;
  return false;
}

}  // namespace

int main(int argc, char** argv) {
  int frames_to_render = 120;
  std::string screenshot;
  for (int i = 1; i + 1 < argc; i += 2) {
    if (std::strcmp(argv[i], "--frames") == 0) {
      frames_to_render = std::atoi(argv[i + 1]);
    } else if (std::strcmp(argv[i], "--screenshot") == 0) {
      screenshot = argv[i + 1];
    }
  }

  App app;
  if (!CreateInstance(&app) || !PickPhysicalDevice(&app) || !CreateDevice(&app) ||
      !CreateDepthResources(&app) || !CreateShadowResources(&app) || !CreatePipelines(&app)) {
    std::fprintf(stderr, "初始化失败\n");
    return 1;
  }

  for (int frame = 0; frame < frames_to_render; ++frame) {
    if (!RecordFrame(&app, static_cast<uint32_t>(frame % 2))) {
      std::fprintf(stderr, "渲染失败\n");
      return 1;
    }
    // TODO(3): 真正呈现时用 vkQueuePresentKHR；同时按 S 键保存截图。
  }

  if (!screenshot.empty()) {
    std::vector<unsigned char> rgba(static_cast<size_t>(kWidth) * kHeight * 4, 0);
    if (!SaveScreenshotPPM(screenshot.c_str(), rgba, kWidth, kHeight)) {
      std::fprintf(stderr, "截图写入失败：%s\n", screenshot.c_str());
      return 1;
    }
  }

  vkDeviceWaitIdle(app.device);
  return 0;
}
