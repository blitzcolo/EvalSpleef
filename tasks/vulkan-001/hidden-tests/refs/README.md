# 参考截图放置说明

`hidden-tests/refs/scene_reference.ppm» 是**参考渲染结果**（P6，800x600），只由 GPU 宿主机生成一次，
随后作为隐藏资产随题包发布（不进 Agent 镜像）。

生成方式（与参考解 A 相同）：

~~~bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
./build/vulkan_shadow --frames 120 --screenshot out/scene.ppm
cp out/scene.ppm <题包>/hidden-tests/refs/scene_reference.ppm
~~~

说明：参考图必须来自**与候选相同驱动/分辨率**的渲染；不同 GPU 的 AA 差异由
`image_metrics.py» 的阈值吸收（MAD ≤ 12、逐通道匹配率 ≥ 0.92）。
未提供参考图时，隐藏验收会打印 SKIP 并跳过截图比对（不影响静态与 CPU 数学段）。
TODO(run-on-host): 在 GPU 节点生成参考图并复核阈值是否合理。
