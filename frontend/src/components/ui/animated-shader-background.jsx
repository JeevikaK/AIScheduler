import { useEffect, useRef } from "react";
import * as THREE from "three";

export default function AnimatedShaderBackground() {
  const containerRef = useRef(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
    container.appendChild(renderer.domElement);

    const material = new THREE.ShaderMaterial({
      transparent: true,
      uniforms: {
        iTime: { value: 0 },
        iResolution: { value: new THREE.Vector2(1, 1) },
      },
      vertexShader: `
        void main() {
          gl_Position = vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform float iTime;
        uniform vec2 iResolution;

        float hash(float n) {
          return fract(sin(n) * 43758.5453123);
        }

        float segmentDistance(vec2 p, vec2 a, vec2 b) {
          vec2 pa = p - a;
          vec2 ba = b - a;
          float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
          return length(pa - ba * h);
        }

        vec3 starColor(float seed) {
          vec3 cyan = vec3(0.18, 0.94, 1.0);
          vec3 blue = vec3(0.21, 0.42, 1.0);
          vec3 violet = vec3(0.47, 0.16, 1.0);

          float tone = hash(seed * 1.73);
          vec3 mixA = mix(cyan, blue, smoothstep(0.0, 1.0, tone));
          return mix(mixA, violet, smoothstep(0.62, 1.0, tone));
        }

        void main() {
          vec2 uv = gl_FragCoord.xy / iResolution.xy;
          vec2 suv = uv;
          vec2 direction = normalize(vec2(1.0, 0.63));
          vec3 base = vec3(0.0011, 0.0025, 0.009);
          vec3 finalColor = base;

          for (float i = 0.0; i < 28.0; i++) {
            float seed = i + 1.0;
            float progress = fract(iTime * (0.028 + hash(seed * 2.7) * 0.04) + hash(seed * 9.1));
            vec2 origin = vec2(
              mix(-0.48, 0.98, hash(seed * 3.17)),
              mix(0.08, 1.28, hash(seed * 4.93))
            );

            vec2 head = origin + direction * progress * 1.9;
            float lengthFactor = 0.2 + hash(seed * 5.31) * 0.28;
            vec2 tail = head - direction * lengthFactor;

            float width = 0.0012 + hash(seed * 6.41) * 0.0022;
            float dist = segmentDistance(suv, tail, head);
            float lineGlow = exp(-pow(dist / width, 1.18));

            float segmentLength = max(length(head - tail), 0.0001);
            float along = clamp(dot(suv - tail, direction) / segmentLength, 0.0, 1.0);
            float headBias = pow(along, 3.8);
            float tailFade = pow(1.0 - along, 0.68);

            float headRadius = width * 2.7;
            float headGlow = exp(-pow(length(suv - head) / headRadius, 1.5));
            float outerHalo = exp(-pow(dist / (width * 5.3), 1.15)) * tailFade;

            vec3 color = starColor(seed);
            float sparkle = 0.7 + 0.7 * sin(iTime * (1.7 + hash(seed * 11.0) * 2.2) + seed * 4.2);

            finalColor += color * lineGlow * mix(0.06, 1.0, headBias) * 0.96;
            finalColor += color * headGlow * (0.96 + sparkle * 0.24);
            finalColor += color * outerHalo * 0.03;
          }

          float vignette = smoothstep(1.1, 0.18, distance(uv, vec2(0.5)));
          finalColor *= vignette;
          finalColor += vec3(0.0012, 0.0022, 0.0075) * exp(-16.0 * distance(uv, vec2(0.52, 0.32)));
          finalColor = min(finalColor, vec3(1.0));

          gl_FragColor = vec4(finalColor, 0.95);
        }
      `,
    });

    const geometry = new THREE.PlaneGeometry(2, 2);
    const mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);

    const resize = () => {
      const width = container.clientWidth || window.innerWidth;
      const height = container.clientHeight || window.innerHeight;
      renderer.setSize(width, height, false);
      material.uniforms.iResolution.value.set(width, height);
    };

    resize();

    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);

    let frameId = 0;
    const animate = () => {
      material.uniforms.iTime.value += 0.016;
      renderer.render(scene, camera);
      frameId = window.requestAnimationFrame(animate);
    };
    animate();

    return () => {
      window.cancelAnimationFrame(frameId);
      resizeObserver.disconnect();
      geometry.dispose();
      material.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
    };
  }, []);

  return (
    <div className="shader-background" aria-hidden="true">
      <div ref={containerRef} className="shader-background-canvas" />
      <div className="shader-background-veil" />
      <div className="shader-background-noise" />
    </div>
  );
}
