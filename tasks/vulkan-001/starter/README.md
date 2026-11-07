# Vulkan 阴影场景（C++ 骨架）

目标：用 Vulkan 从零搭一条渲染管线，画出「地面 + 一个方块 + 一束方向光」的场景，
在开启验证层的情况下**零 error**；平台会拿一张参考截图做相似度比对。

## 目录

    shaders/scene.vert / scene.frag   # 主通道着色器（frag 目前没有阴影）
    shaders/shadow.vert / shadow.frag # 深度通道着色器（可选结构，A/B 两种做法都接受）
    src/shadow_math.h                 # 阴影数学接口契约（**不得修改**，哈希锁定）
    src/shadow_math.cpp               # CPU 侧阴影矩阵/PCF 实现（当前是空壳）
    src/main.cpp                      # 实例/设备/交换链/管线骨架（缺深度资源与验证层）
    CMakeLists.txt

## 要求

- 必须启用 Khronos 验证层 `VK_LAYER_KHRONOS_validation»，并安装 debug messenger；验证层输出 error 即失败。
- 阴影实现方式不限：单 pass 在主着色器里直接采样深度，或两 pass 先渲染深度再主 pass 采样。
- 阴影数学（光源视图/投影矩阵、3x3 PCF）必须放在 `src/shadow_math.cpp» 里，且能被普通 host 编译器单独编译。
- 窗口 800x600，按 `S» 键保存一帧到 `out/scene.ppm»（P6，无压缩）；平台会读这个文件。
- 场景内容：深灰地面、青色方块、方向光从左上角打向右下；方块在地面上要有可见阴影。

## 构建/运行（宿主机需要 Vulkan SDK + glslc）

~~~bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/vulkan_shadow --frames 120 --screenshot out/scene.ppm
~~~

无 GPU 时可用 Mesa lavapipe 软件实现跑功能验证（`VK_ICD_FILENAMES» 指向 lavapipe icd）；
性能数据不允许在 lavapipe 上采。
