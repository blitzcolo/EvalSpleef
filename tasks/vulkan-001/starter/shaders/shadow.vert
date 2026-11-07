#version 450

layout(location = 0) in vec3 in_position;

layout(set = 0, binding = 2) uniform ShadowUniforms {
  mat4 light_view_proj;
} u;

void main() {
  gl_Position = u.light_view_proj * vec4(in_position, 1.0);
}
