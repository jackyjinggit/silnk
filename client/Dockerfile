FROM olbat/cupsd

ENV SERVER_HOST=""
ENV PRINTER_URL=""
ENV SECRET_KEY=""

# cups-client 提供 lpstat / lpoptions（客户端自动识别型号与能力要用）
# cups-ipp-utils 提供 ipptool（Linux 出纸链路必须）
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 cups-client cups-ipp-utils \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chmod +x start.sh

CMD "./start.sh"
