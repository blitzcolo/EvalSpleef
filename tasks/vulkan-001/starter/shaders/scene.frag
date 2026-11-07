#version 450

layout(location = 0) in vec3 v_world_pos;
layout(location = 1) in vec3 v_normal;
layout(location = 2) in vec4 v_light_space_pos;

layout(set = 0, binding = 1) uniform sampler2DShadow u_shadow_map;

layout(location = 0) out vec4 out_color;

const vec3 k_ground = vec3(0.22, 0.22, 0.24);
const vec3 k_cube = vec3(0.20, 0.75, 0.78);

float pcf_shadow(vec4 light_space_pos, float ndotl) {
  // TODO: 归一化到 [0,1]，越界返回 1.0（受光）；用 3x3 PCF（textureOffset 或循环 + texture()）。
  return 1.0;
}

void main() {
  vec3 albedo = k_ground;
  if (abs(v_normal.y) < 0.5) {
    albedo = k_cube;
  }
  vec3 n = normalize(v_normal);
  float ndotl = max(dot(n, normalize(vec3(0.4, 0.8, 0.3))), 0.0);
  float shadow = pcf_shadow(v_light_space_pos, ndotl);
  vec3 color = albedo * (0.25 + 0.75 * ndotl * shadow);
  out_color = vec4(color, 1.0);
}
