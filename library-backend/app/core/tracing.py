# OpenTelemetry 트레이싱 설정 (Jaeger 연동)

import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

logger = logging.getLogger(__name__)


def setup_tracing(service_name: str = None):
    """
    OpenTelemetry 트레이싱 초기화
    
    Args:
        service_name: 서비스 이름 (기본값: 환경변수 OTEL_SERVICE_NAME 또는 "library-backend")
    
    Returns:
        Tracer 인스턴스
    """
    service = service_name or os.getenv("OTEL_SERVICE_NAME", "library-backend")
    
    # 리소스 정보 설정
    resource = Resource.create({
        SERVICE_NAME: service,
        "service.version": os.getenv("APP_VERSION", "1.0.0"),
        "deployment.environment": os.getenv("ENV", "production"),
    })
    
    # TracerProvider 생성
    provider = TracerProvider(resource=resource)
    
    # OTLP Exporter 설정 (Jaeger Collector로 전송)
    otlp_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", 
        "http://jaeger.istio-system.svc.cluster.local:4317"
    )
    
    otlp_exporter = OTLPSpanExporter(
        endpoint=otlp_endpoint,
        insecure=True  # gRPC TLS 비활성화 (클러스터 내부 통신)
    )
    
    # BatchSpanProcessor로 효율적인 전송
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    
    # 전역 TracerProvider 설정
    trace.set_tracer_provider(provider)
    
    logger.info(f"✅ OpenTelemetry 초기화 완료 - service: {service}, endpoint: {otlp_endpoint}")
    
    return trace.get_tracer(__name__)