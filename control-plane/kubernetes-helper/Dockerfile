FROM python:3.14-slim
WORKDIR /opt/flumic
COPY flumic_job_helper.py /opt/flumic/flumic_job_helper.py
USER 10001:10001
ENTRYPOINT ["python", "-I", "-B", "/opt/flumic/flumic_job_helper.py"]
