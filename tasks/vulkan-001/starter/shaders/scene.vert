#version 450

layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_normal;

layout(set = 0, binding = 0) uniform SceneUniforms {
  mat4 view_proj;
  mat4 light_view_proj;
  vec3 light_dir;
  vec3 camera_pos;
} u;

layout(location = 0) out vec3 v_world_pos;
layout(location = 1) out vec3 v_normal;
layout(location = 2) out vec4 v_light_space_pos;

void main() {
  v_world_pos = in_position;
  v_normal = in_normal;
  v_light_space_pos = u.light_view_proj * vec4(in_position, 1.0);
  gl_Position = u.view_proj * vec4(in_position, 1.0);
}
