package worker

import (
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type httpClients struct {
	fetch, send *http.Client
}

func newHTTPClients(s networkSettings) (httpClients, error) {
	proxies := make(map[string]*url.URL, 2)
	for scheme, value := range map[string]string{"http": s.HTTPProxy, "https": s.HTTPSProxy} {
		if value == "" {
			continue
		}
		proxy, err := url.Parse(value)
		if err != nil || proxy.Hostname() == "" || (proxy.Scheme != "http" && proxy.Scheme != "https") {
			return httpClients{}, fmt.Errorf("%s 代理地址无效", scheme)
		}
		proxies[scheme] = proxy
	}
	excluded := strings.Split(s.NoProxy, ",")
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxConnsPerHost = 22
	transport.DialContext = (&net.Dialer{
		Timeout: time.Duration(s.ConnectTimeout) * time.Second, KeepAlive: 30 * time.Second,
	}).DialContext
	transport.Proxy = func(req *http.Request) (*url.URL, error) {
		host := strings.ToLower(req.URL.Hostname())
		for _, entry := range excluded {
			exclude := strings.Trim(strings.ToLower(strings.TrimSpace(entry)), ".[]")
			if exclude == "" {
				continue
			}
			if exclude == "*" || host == exclude || (net.ParseIP(exclude) == nil && strings.HasSuffix(host, "."+exclude)) {
				return nil, nil
			}
		}
		if proxy := proxies[req.URL.Scheme]; proxy != nil {
			return proxy, nil
		}
		return http.ProxyFromEnvironment(req)
	}
	return httpClients{
		fetch: &http.Client{Transport: transport, CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) > 5 {
				return fmt.Errorf("订阅重定向次数超限")
			}
			return nil
		}},
		send: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
	}, nil
}

func (c httpClients) close() {
	c.fetch.CloseIdleConnections()
	c.send.CloseIdleConnections()
}
