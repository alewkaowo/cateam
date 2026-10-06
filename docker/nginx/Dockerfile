FROM alpine:3.24 AS element
ARG ELEMENT_VERSION=v1.12.30
RUN apk add --no-cache curl tar
RUN curl --fail --location --retry 3 https://github.com/element-hq/element-web/releases/download/${ELEMENT_VERSION}/element-${ELEMENT_VERSION}.tar.gz -o /tmp/element.tar.gz \
    && mkdir /element && tar -xzf /tmp/element.tar.gz --strip-components=1 -C /element \
    && test -s /element/index.html && test -s /element/widgets/element-call/index.html
FROM nginx:1.30.4-alpine
COPY --from=element /element/ /usr/share/nginx/html/
COPY config/nginx.conf /etc/nginx/nginx.conf
STOPSIGNAL SIGQUIT
