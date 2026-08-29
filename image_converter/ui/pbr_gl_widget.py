from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent, QSurfaceFormat, QWheelEvent
from PyQt6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFunctions_2_0,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

from image_converter.services.pbr_preview import (
    PBR_GEOMETRY_PLANE,
    PBR_NORMAL_AUTO,
    PBR_NORMAL_DIRECTX,
    PBR_SOLO_AO,
    PBR_SOLO_BASECOLOR,
    PBR_SOLO_BEAUTY,
    PBR_SOLO_METALLIC,
    PBR_SOLO_NORMAL,
    PBR_SOLO_NORMAL_CHECK,
    PBR_SOLO_ROUGHNESS,
    PbrMaterialData,
)
from image_converter.ui.common import _qimage_from_pil


_VERTEX_SHADER = """
#version 110
attribute vec2 aPosition;
varying vec2 vPosition;

void main()
{
    vPosition = aPosition;
    gl_Position = vec4(aPosition, 0.0, 1.0);
}
"""


_FRAGMENT_SHADER = """
#version 110
varying vec2 vPosition;

uniform sampler2D uBaseColor;
uniform sampler2D uNormal;
uniform sampler2D uProperties;
uniform sampler2D uEmissive;
uniform float uAspect;
uniform float uObjectScale;
uniform vec2 uPan;
uniform float uLightAngle;
uniform float uLightElevation;
uniform float uObjectYaw;
uniform float uObjectPitch;
uniform int uGeometry;
uniform int uSolo;
uniform int uDirectXNormal;

const float PI = 3.14159265359;

float geometrySchlick(float value, float k)
{
    return value / max(value * (1.0 - k) + k, 0.00001);
}

vec3 backgroundColor()
{
    float gradient = clamp(vPosition.y * 0.5 + 0.5, 0.0, 1.0);
    return mix(vec3(0.055, 0.066, 0.078), vec3(0.105, 0.125, 0.145), gradient);
}

vec3 rotateX(vec3 value, float angle)
{
    float sine = sin(angle);
    float cosine = cos(angle);
    return vec3(
        value.x,
        cosine * value.y - sine * value.z,
        sine * value.y + cosine * value.z
    );
}

vec3 rotateY(vec3 value, float angle)
{
    float sine = sin(angle);
    float cosine = cos(angle);
    return vec3(
        cosine * value.x + sine * value.z,
        value.y,
        -sine * value.x + cosine * value.z
    );
}

void main()
{
    vec3 background = backgroundColor();
    vec2 point = vPosition - uPan;
    if (uAspect > 1.0)
        point.x *= uAspect;
    else
        point.y /= max(uAspect, 0.001);
    point /= uObjectScale;

    vec2 uv;
    vec3 geometricNormal;
    vec3 tangent;
    vec3 bitangent;

    if (uGeometry == 1) {
        if (abs(point.x) > 1.0 || abs(point.y) > 1.0) {
            gl_FragColor = vec4(background, 1.0);
            return;
        }
        uv = vec2(point.x * 0.5 + 0.5, 0.5 - point.y * 0.5);
        geometricNormal = vec3(0.0, 0.0, 1.0);
        tangent = vec3(1.0, 0.0, 0.0);
        bitangent = vec3(0.0, 1.0, 0.0);
    } else {
        float radiusSquared = dot(point, point);
        if (radiusSquared > 1.0) {
            gl_FragColor = vec4(background, 1.0);
            return;
        }
        float z = sqrt(max(0.0, 1.0 - radiusSquared));
        geometricNormal = vec3(point.x, point.y, z);
        vec3 localNormal = rotateX(
            rotateY(geometricNormal, -uObjectYaw),
            -uObjectPitch
        );
        uv = vec2(
            mod(0.5 + atan(localNormal.x, localNormal.z) / (2.0 * PI), 1.0),
            0.5 - asin(clamp(localNormal.y, -1.0, 1.0)) / PI
        );
        float horizontal = max(length(vec2(localNormal.x, localNormal.z)), 0.0001);
        vec3 localTangent = vec3(
            localNormal.z / horizontal,
            0.0,
            -localNormal.x / horizontal
        );
        vec3 localBitangent = vec3(
            -localNormal.x * localNormal.y / horizontal,
            horizontal,
            -localNormal.y * localNormal.z / horizontal
        );
        tangent = rotateY(rotateX(localTangent, uObjectPitch), uObjectYaw);
        bitangent = rotateY(rotateX(localBitangent, uObjectPitch), uObjectYaw);
    }

    vec4 baseSample = texture2D(uBaseColor, uv);
    vec3 normalSample = texture2D(uNormal, uv).rgb;
    vec4 properties = texture2D(uProperties, uv);
    vec3 emissiveSample = texture2D(uEmissive, uv).rgb;
    bool flipGreen = uDirectXNormal == 1;
    if (uSolo == 6 && vPosition.x > 0.0)
        flipGreen = !flipGreen;
    if (flipGreen)
        normalSample.g = 1.0 - normalSample.g;

    vec3 color;
    if (uSolo == 1) {
        color = baseSample.rgb;
    } else if (uSolo == 2) {
        color = normalSample;
    } else if (uSolo == 3) {
        color = vec3(properties.r);
    } else if (uSolo == 4) {
        color = vec3(properties.g);
    } else if (uSolo == 5) {
        color = vec3(properties.b);
    } else {
        vec3 tangentNormal = normalize(normalSample * 2.0 - 1.0);
        vec3 normal = normalize(
            tangent * tangentNormal.x
            + bitangent * tangentNormal.y
            + geometricNormal * tangentNormal.z
        );
        vec3 light = normalize(vec3(
            cos(uLightElevation) * sin(uLightAngle),
            sin(uLightElevation),
            cos(uLightElevation) * cos(uLightAngle)
        ));
        vec3 view = vec3(0.0, 0.0, 1.0);
        vec3 halfVector = normalize(light + view);
        float nDotL = max(dot(normal, light), 0.0);
        float nDotV = max(dot(normal, view), 0.0001);
        float nDotH = max(dot(normal, halfVector), 0.0);
        float vDotH = max(dot(view, halfVector), 0.0);
        float roughness = max(properties.r, 0.04);
        float metallic = properties.g;
        float ao = properties.b;

        float alpha = roughness * roughness;
        float alphaSquared = alpha * alpha;
        float denominator = nDotH * nDotH * (alphaSquared - 1.0) + 1.0;
        float distribution = alphaSquared / max(PI * denominator * denominator, 0.00001);
        float k = (roughness + 1.0) * (roughness + 1.0) / 8.0;
        float geometry = geometrySchlick(nDotV, k) * geometrySchlick(nDotL, k);

        vec3 baseLinear = pow(baseSample.rgb, vec3(2.2));
        vec3 f0 = mix(vec3(0.04), baseLinear, metallic);
        vec3 fresnel = f0 + (1.0 - f0) * pow(1.0 - vDotH, 5.0);
        vec3 specular = distribution * geometry * fresnel
            / max(4.0 * nDotV * max(nDotL, 0.0001), 0.0001);
        vec3 diffuse = (1.0 - fresnel) * (1.0 - metallic) * baseLinear / PI;
        vec3 ambient = baseLinear * (0.035 + 0.18 * ao);
        vec3 emissive = pow(emissiveSample, vec3(2.2));
        vec3 linearColor = ambient + (diffuse + specular) * nDotL * 3.4 + emissive;
        color = pow(1.0 - exp(-max(linearColor, vec3(0.0))), vec3(1.0 / 2.2));
    }

    float opacity = properties.a * baseSample.a;
    vec3 finalColor = mix(background, color, opacity);
    if (uSolo == 6 && abs(vPosition.x) < 0.0035)
        finalColor = vec3(0.22, 0.68, 1.0);
    gl_FragColor = vec4(finalColor, 1.0);
}
"""


class PbrOpenGLWidget(QOpenGLWidget):
    initialization_failed = pyqtSignal(str)

    _OBJECT_DRAG_SENSITIVITY = 0.25
    _LIGHT_DRAG_SENSITIVITY = 0.35

    _SOLO_VALUES = {
        PBR_SOLO_BEAUTY: 0,
        PBR_SOLO_BASECOLOR: 1,
        PBR_SOLO_NORMAL: 2,
        PBR_SOLO_ROUGHNESS: 3,
        PBR_SOLO_METALLIC: 4,
        PBR_SOLO_AO: 5,
        PBR_SOLO_NORMAL_CHECK: 6,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        surface_format = QSurfaceFormat()
        surface_format.setVersion(2, 0)
        surface_format.setProfile(QSurfaceFormat.OpenGLContextProfile.NoProfile)
        surface_format.setDepthBufferSize(0)
        surface_format.setStencilBufferSize(0)
        self.setFormat(surface_format)
        self.setMinimumSize(240, 240)
        self.setAutoFillBackground(False)

        self._functions: QOpenGLFunctions_2_0 | None = None
        self._program: QOpenGLShaderProgram | None = None
        self._vertex_buffer: QOpenGLBuffer | None = None
        self._textures: list[QOpenGLTexture] = []
        self._material: PbrMaterialData | None = None
        self._uploaded_material: PbrMaterialData | None = None
        self._geometry = "sphere"
        self._solo = PBR_SOLO_BEAUTY
        self._normal_convention = PBR_NORMAL_AUTO
        self._light_rotation = -35.0
        self._light_elevation = 42.0
        self._rotation_yaw = 0.0
        self._rotation_pitch = 0.0
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_position: QPointF | None = None
        self._drag_mode: str | None = None
        self._initialization_error = ""
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    @property
    def material(self) -> PbrMaterialData | None:
        return self._material

    @property
    def initialization_error(self) -> str:
        return self._initialization_error

    @property
    def light_rotation(self) -> float:
        return self._light_rotation

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_material(self, material: PbrMaterialData | None) -> None:
        self._material = material
        self.update()

    def set_geometry(self, geometry: str) -> None:
        self._geometry = geometry
        self.update()

    def set_solo(self, solo: str) -> None:
        self._solo = solo
        self.update()

    def set_normal_convention(self, convention: str) -> None:
        self._normal_convention = convention
        self.update()

    def set_light_rotation(self, degrees: float) -> None:
        self._light_rotation = ((float(degrees) + 180.0) % 360.0) - 180.0
        self.update()

    def set_light_orientation(self, azimuth: float, elevation: float) -> None:
        self._light_rotation = ((float(azimuth) + 180.0) % 360.0) - 180.0
        self._light_elevation = max(-5.0, min(float(elevation), 85.0))
        self.update()

    def reset_light_orientation(self) -> None:
        self._light_rotation = -35.0
        self._light_elevation = 42.0
        self.update()

    def set_object_rotation(self, yaw: float, pitch: float) -> None:
        self._rotation_yaw = float(yaw) % 360.0
        self._rotation_pitch = max(-89.0, min(float(pitch), 89.0))
        self.update()

    def reset_object_rotation(self) -> None:
        self.set_object_rotation(0.0, 0.0)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.35, min(float(zoom), 3.0))
        self.update()

    def set_pan(self, x: float, y: float) -> None:
        self._pan_x = max(-2.0, min(float(x), 2.0))
        self._pan_y = max(-2.0, min(float(y), 2.0))
        self.update()

    def reset_object_view(self) -> None:
        self._rotation_yaw = 0.0
        self._rotation_pitch = 0.0
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()

    def initializeGL(self) -> None:
        try:
            self._uploaded_material = None
            self.context().aboutToBeDestroyed.connect(self.cleanup)
            self._functions = QOpenGLFunctions_2_0()
            if not self._functions.initializeOpenGLFunctions():
                raise RuntimeError("OpenGL 2.0 functions are unavailable.")

            program = QOpenGLShaderProgram(self)
            if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex,
                _VERTEX_SHADER,
            ):
                raise RuntimeError(program.log())
            if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment,
                _FRAGMENT_SHADER,
            ):
                raise RuntimeError(program.log())
            program.bindAttributeLocation("aPosition", 0)
            if not program.link():
                raise RuntimeError(program.log())
            self._program = program

            vertices = (
                -1.0, -1.0,
                1.0, -1.0,
                1.0, 1.0,
                -1.0, -1.0,
                1.0, 1.0,
                -1.0, 1.0,
            )
            import struct

            vertex_data = struct.pack(f"{len(vertices)}f", *vertices)
            self._vertex_buffer = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            if not self._vertex_buffer.create():
                raise RuntimeError("Could not create the OpenGL vertex buffer.")
            self._vertex_buffer.bind()
            self._vertex_buffer.allocate(vertex_data, len(vertex_data))
            self._vertex_buffer.release()
        except Exception as exc:
            self._initialization_error = str(exc) or type(exc).__name__
            self.initialization_failed.emit(self._initialization_error)

    def resizeGL(self, width: int, height: int) -> None:
        if self._functions is not None:
            ratio = self.devicePixelRatioF()
            self._functions.glViewport(
                0,
                0,
                max(1, round(width * ratio)),
                max(1, round(height * ratio)),
            )

    def paintGL(self) -> None:
        if self._functions is None:
            return
        self._functions.glClearColor(0.055, 0.066, 0.078, 1.0)
        self._functions.glClear(0x00004000)
        if self._program is None or self._vertex_buffer is None:
            return
        if self._material is not self._uploaded_material:
            self._upload_material()
        if not self._textures:
            return

        program = self._program
        program.bind()
        for unit, texture in enumerate(self._textures):
            texture.bind(unit)
        program.setUniformValue("uBaseColor", 0)
        program.setUniformValue("uNormal", 1)
        program.setUniformValue("uProperties", 2)
        program.setUniformValue("uEmissive", 3)
        program.setUniformValue("uAspect", self.width() / max(self.height(), 1))
        program.setUniformValue("uObjectScale", 0.88 * self._zoom)
        program.setUniformValue("uPan", self._pan_x, self._pan_y)
        program.setUniformValue("uLightAngle", math.radians(self._light_rotation))
        program.setUniformValue("uLightElevation", math.radians(self._light_elevation))
        program.setUniformValue("uObjectYaw", math.radians(self._rotation_yaw))
        program.setUniformValue("uObjectPitch", math.radians(self._rotation_pitch))
        program.setUniformValue(
            "uGeometry",
            1 if self._geometry == PBR_GEOMETRY_PLANE else 0,
        )
        program.setUniformValue("uSolo", self._SOLO_VALUES.get(self._solo, 0))
        program.setUniformValue("uDirectXNormal", int(self._uses_directx_normal()))

        self._vertex_buffer.bind()
        program.enableAttributeArray(0)
        program.setAttributeBuffer(0, 0x1406, 0, 2, 0)
        self._functions.glDrawArrays(0x0004, 0, 6)
        program.disableAttributeArray(0)
        self._vertex_buffer.release()
        for texture in self._textures:
            texture.release()
        program.release()

    def _uses_directx_normal(self) -> bool:
        if self._normal_convention == PBR_NORMAL_DIRECTX:
            return True
        if self._normal_convention != PBR_NORMAL_AUTO:
            return False
        return bool(self._material and self._material.normal_is_directx)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
        ):
            self._drag_position = event.position()
            if event.button() == Qt.MouseButton.MiddleButton:
                self._drag_mode = "pan"
            else:
                self._drag_mode = (
                    "light"
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier
                    else "object"
                )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._drag_position is not None
            and event.buttons()
            & (Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton)
        ):
            delta = event.position() - self._drag_position
            self._drag_position = event.position()
            if self._drag_mode == "pan":
                self.set_pan(
                    self._pan_x + 2.0 * delta.x() / max(self.width(), 1),
                    self._pan_y - 2.0 * delta.y() / max(self.height(), 1),
                )
            elif self._drag_mode == "light":
                self.set_light_orientation(
                    self._light_rotation
                    + delta.x() * self._LIGHT_DRAG_SENSITIVITY,
                    self._light_elevation
                    - delta.y() * self._LIGHT_DRAG_SENSITIVITY,
                )
            else:
                self.set_object_rotation(
                    self._rotation_yaw
                    + delta.x() * self._OBJECT_DRAG_SENSITIVITY,
                    self._rotation_pitch
                    + delta.y() * self._OBJECT_DRAG_SENSITIVITY,
                )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
        ):
            self._drag_position = None
            self._drag_mode = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.reset_light_orientation()
            else:
                self.reset_object_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.set_zoom(self._zoom * (1.12**steps))
            event.accept()
            return
        super().wheelEvent(event)

    def _upload_material(self) -> None:
        for texture in self._textures:
            texture.destroy()
        self._textures.clear()
        self._uploaded_material = self._material
        if self._material is None:
            return

        for image in (
            self._material.basecolor,
            self._material.normal,
            self._material.properties,
            self._material.emissive,
        ):
            texture = QOpenGLTexture(
                _qimage_from_pil(image),
                QOpenGLTexture.MipMapGeneration.GenerateMipMaps,
            )
            texture.setWrapMode(QOpenGLTexture.WrapMode.Repeat)
            texture.setMinificationFilter(
                QOpenGLTexture.Filter.LinearMipMapLinear
            )
            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            self._textures.append(texture)

    def cleanup(self) -> None:
        if not self.isValid():
            return
        self.makeCurrent()
        for texture in self._textures:
            texture.destroy()
        self._textures.clear()
        if self._vertex_buffer is not None:
            self._vertex_buffer.destroy()
        self._uploaded_material = None
        self.doneCurrent()
