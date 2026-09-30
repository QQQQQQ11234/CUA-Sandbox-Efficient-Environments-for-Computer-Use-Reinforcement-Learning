<?php
declare(strict_types=1);

/**
 * Trusted request router for the shared WebArena Magento runtime.
 *
 * The browser supplies only a signed capability. Database names, endpoints,
 * filesystem roots, Redis prefixes, and search indexes come exclusively from
 * the loopback route registry.
 */
final class WebAgentMagentoRoute
{
    private static ?array $route = null;
    private static bool $leased = false;

    public static function bootstrap(): void
    {
        if (self::$route !== null) {
            return;
        }
        if (getenv('WEB_AGENT_ALLOW_UNROUTED') === '1' && self::token() === '') {
            return;
        }

        try {
            $agentId = self::verifyToken(self::token());
            $maintenance = getenv('WEB_AGENT_ROUTE_MAINTENANCE') === '1';
            self::$route = self::registryRequest(
                $agentId,
                $maintenance ? null : 'acquire'
            );
            self::$leased = !$maintenance;
            self::validateRoute(self::$route, $agentId);
            self::installCanonicalRequestAuthority(self::$route);
            self::installDirectoryOverrides(self::$route);
            unset(
                $_SERVER['HTTP_X_AGENT_ROUTE'],
                $_SERVER['HTTP_X_AGENT_ID'],
                $_SERVER['HTTP_X_AGENT_DB']
            );
            if (self::$leased) {
                register_shutdown_function([self::class, 'release']);
            }
        } catch (Throwable $error) {
            // A lease is acquired before filesystem/config validation.  Any
            // later bootstrap failure must release it or lifecycle freeze
            // would correctly (but permanently) refuse to drain.
            if (self::$leased) {
                self::release();
            }
            self::deny($error);
        }
    }

    public static function applyDeploymentConfig(array $config): array
    {
        self::bootstrap();
        if (self::$route === null) {
            return $config;
        }
        $route = self::$route;
        $config['db']['connection']['default']['host'] =
            $route['db_host'] . ':' . (string) $route['db_port'];
        $config['db']['connection']['default']['dbname'] = $route['db_name'];

        $siteKey = strtoupper((string) $route['site']);
        $siteKey = str_replace('-', '_', $siteKey);
        $cryptKey = (string) (
            getenv('WEB_AGENT_MAGENTO_CRYPT_KEY_' . $siteKey) ?: ''
        );
        if ($cryptKey === '') {
            throw new RuntimeException(
                'Magento crypt key is not configured for routed site ' . $route['site']
            );
        }
        $config['crypt']['key'] = $cryptKey;

        // Routed deployment values intentionally differ per request. Magento's
        // static env.php hash guard would otherwise reject this supported
        // blue/green-style configuration switch before dispatch.
        $config['deployment']['blue_green']['enabled'] = 1;

        $baseUrl = 'http://' . self::canonicalAuthority($route) . '/';
        $config['system']['default']['web']['unsecure']['base_url'] = $baseUrl;
        $config['system']['default']['web']['secure']['base_url'] = $baseUrl;

        $environmentCacheId = substr(
            hash('sha256', $route['environment_id'] . ':' . $route['site']),
            0,
            16
        );
        $branchCacheId = substr(hash('sha256', (string) $route['branch_id']), 0, 8);
        $cachePrefix = 'wa_' . $environmentCacheId . '_' . $branchCacheId
            . '_g' . (int) $route['generation'] . '_';
        foreach (['default', 'page_cache'] as $frontend) {
            if (isset($config['cache']['frontend'][$frontend])) {
                $config['cache']['frontend'][$frontend]['id_prefix'] = $cachePrefix;
            }
        }

        $stateRoot = self::branchStateRoot($route);
        $sessionRoot = $stateRoot . '/sessions';
        self::ensureDirectory($sessionRoot);
        $config['session'] = [
            'save' => 'files',
            'save_path' => $sessionRoot,
        ];

        // Magento reads these paths from deployment config before falling back
        // to core_config_data. Cover both Elasticsearch and OpenSearch names
        // used by supported Magento 2.4 patch releases.
        foreach (
            [
                'elasticsearch6_index_prefix',
                'elasticsearch7_index_prefix',
                'opensearch_index_prefix',
            ] as $key
        ) {
            $config['system']['default']['catalog']['search'][$key] =
                $route['opensearch_index'];
        }
        return $config;
    }

    public static function release(): void
    {
        if (!self::$leased || self::$route === null) {
            return;
        }
        self::$leased = false;
        try {
            self::registryRequest((string) self::$route['agent_id'], 'release');
        } catch (Throwable $error) {
            error_log('WebAgent route release failed: ' . $error->getMessage());
        }
    }

    public static function current(): ?array
    {
        return self::$route;
    }

    public static function mediaPath(string $requestPath): string
    {
        self::bootstrap();
        if (self::$route === null) {
            throw new RuntimeException('no routed Magento environment');
        }
        $prefix = '/media/';
        if (!str_starts_with($requestPath, $prefix)) {
            throw new InvalidArgumentException('invalid media request path');
        }
        $relative = rawurldecode(substr($requestPath, strlen($prefix)));
        if ($relative === '' || str_contains($relative, "\0")) {
            throw new InvalidArgumentException('invalid media file');
        }
        $root = self::branchStateRoot(self::$route) . '/media';
        $target = realpath($root . '/' . $relative);
        $rootReal = realpath($root);
        if ($target !== false && $rootReal !== false
            && str_starts_with($target, $rootReal . DIRECTORY_SEPARATOR)
            && is_file($target)
        ) {
            return $target;
        }
        // The large storefront baseline remains immutable in the shared image;
        // branch directories contain only writes. Current reviewed tasks never
        // delete/replace baseline media, so unsupported media mutations remain
        // blocked by the task capability gate.
        $lower = rtrim(
            (string) (getenv('WEB_AGENT_MAGENTO_BASE_MEDIA_ROOT')
                ?: '/var/www/magento2/pub/media'),
            '/'
        );
        $lowerTarget = realpath($lower . '/' . $relative);
        $lowerReal = realpath($lower);
        if ($lowerTarget === false || $lowerReal === false
            || !str_starts_with($lowerTarget, $lowerReal . DIRECTORY_SEPARATOR)
            || !is_file($lowerTarget)
        ) {
            throw new RuntimeException('media file not found');
        }
        return $lowerTarget;
    }

    private static function token(): string
    {
        $token = (string) ($_SERVER['HTTP_X_AGENT_ROUTE'] ?? '');
        if ($token === '') {
            $token = (string) (getenv('WEB_AGENT_ROUTE_TOKEN') ?: '');
        }
        return $token;
    }

    private static function verifyToken(string $token): string
    {
        $secret = (string) (getenv('WEB_AGENT_ROUTE_TOKEN_SECRET') ?: '');
        if (strlen($secret) < 32) {
            throw new RuntimeException('route token secret is not configured');
        }
        $parts = explode('.', $token, 2);
        if (count($parts) !== 2) {
            throw new InvalidArgumentException('missing or malformed route token');
        }
        $payload = self::base64UrlDecode($parts[0]);
        $signature = self::base64UrlDecode($parts[1]);
        $expected = hash_hmac('sha256', $payload, $secret, true);
        if (!hash_equals($expected, $signature)) {
            throw new InvalidArgumentException('invalid route token signature');
        }
        $claims = json_decode($payload, true, 16, JSON_THROW_ON_ERROR);
        if (($claims['v'] ?? null) !== 1 || (int) ($claims['exp'] ?? 0) < time()) {
            throw new InvalidArgumentException('expired or unsupported route token');
        }
        $agentId = (string) ($claims['agent_id'] ?? '');
        if (!preg_match('/\A[A-Za-z0-9_.-]+\z/', $agentId)) {
            throw new InvalidArgumentException('invalid route agent id');
        }
        return $agentId;
    }

    private static function registryRequest(string $agentId, ?string $action): array
    {
        $baseUrl = rtrim(
            (string) (getenv('WEB_AGENT_ROUTE_REGISTRY_URL') ?: 'http://127.0.0.1:8765'),
            '/'
        );
        $secret = (string) (getenv('WEB_AGENT_ROUTE_REGISTRY_SECRET') ?: '');
        if (strlen($secret) < 32) {
            throw new RuntimeException('route registry secret is not configured');
        }
        $url = $baseUrl . '/v1/routes/' . rawurlencode($agentId);
        if ($action !== null) {
            $url .= '/' . $action;
        }
        $headers = [
            'Authorization: Bearer ' . $secret,
            'Accept: application/json',
            'Connection: close',
        ];
        $context = stream_context_create([
            'http' => [
                'method' => $action === null ? 'GET' : 'POST',
                'header' => implode("\r\n", $headers),
                'timeout' => 10,
                'ignore_errors' => true,
            ],
        ]);
        $body = @file_get_contents($url, false, $context);
        $status = 0;
        foreach (($http_response_header ?? []) as $header) {
            if (preg_match('/\AHTTP\/\S+\s+(\d{3})/', $header, $match)) {
                $status = (int) $match[1];
            }
        }
        if ($status === 423) {
            throw new WebAgentMagentoRouteFrozen('route is frozen');
        }
        if ($status !== 200 || $body === false) {
            throw new RuntimeException("route registry returned HTTP {$status}");
        }
        $route = json_decode($body, true, 32, JSON_THROW_ON_ERROR);
        if (!is_array($route)) {
            throw new RuntimeException('route registry returned an invalid record');
        }
        return $route;
    }

    private static function validateRoute(array $route, string $agentId): void
    {
        if (!hash_equals($agentId, (string) ($route['agent_id'] ?? ''))) {
            throw new RuntimeException('route identity mismatch');
        }
        if (($route['db_engine'] ?? '') !== 'mysql') {
            throw new RuntimeException('Magento route does not use MySQL');
        }
        if (!in_array(($route['site'] ?? ''), ['shopping', 'shopping_admin'], true)) {
            throw new RuntimeException('route is not a Shopping route');
        }
        $configuredSite = trim((string) (
            getenv('WEB_AGENT_MAGENTO_SITE') ?: ''
        ));
        if ($configuredSite !== '' && !hash_equals(
            $configuredSite,
            (string) $route['site']
        )) {
            throw new RuntimeException('route site does not match shared Magento image');
        }
        if (!preg_match('/\A[A-Za-z0-9_]+\z/', (string) ($route['db_name'] ?? ''))) {
            throw new RuntimeException('invalid routed database name');
        }
        if (!filter_var((string) ($route['db_host'] ?? ''), FILTER_VALIDATE_IP)
            && !preg_match('/\A[A-Za-z0-9.-]+\z/', (string) ($route['db_host'] ?? ''))
        ) {
            throw new RuntimeException('invalid routed database host');
        }
        $port = (int) ($route['db_port'] ?? 0);
        if ($port < 1 || $port > 65535) {
            throw new RuntimeException('invalid routed database port');
        }
    }

    private static function installCanonicalRequestAuthority(array $route): void
    {
        if (PHP_SAPI === 'cli') {
            return;
        }
        $authority = self::canonicalAuthority($route);
        [$host, $port] = explode(':', $authority, 2);
        $_SERVER['HTTP_HOST'] = $authority;
        $_SERVER['SERVER_NAME'] = $host;
        $_SERVER['SERVER_PORT'] = $port;
        $_SERVER['REQUEST_SCHEME'] = 'http';
        $_SERVER['HTTPS'] = 'off';
    }

    private static function canonicalAuthority(array $route): string
    {
        $site = (string) $route['site'];
        $siteKey = strtoupper(str_replace('-', '_', $site));
        $configured = trim((string) (
            getenv('WEB_AGENT_CANONICAL_AUTHORITY_' . $siteKey) ?: ''
        ));
        if ($configured !== '') {
            if (!preg_match(
                '/\A(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}\z/',
                $configured
            )) {
                throw new RuntimeException(
                    'invalid canonical authority for routed site ' . $site
                );
            }
            return $configured;
        }
        return match ($site) {
            'shopping' => '127.0.0.1:7770',
            'shopping_admin' => '127.0.0.1:7780',
            default => throw new RuntimeException('unsupported routed Magento site'),
        };
    }

    private static function installDirectoryOverrides(array $route): void
    {
        $root = self::branchStateRoot($route);
        $generation = (int) $route['generation'];
        $paths = [
            'media' => $root . '/media',
            'session' => $root . '/sessions',
            'var' => $root . '/var/g' . $generation,
            'tmp' => $root . '/var/g' . $generation . '/tmp',
            'log' => $root . '/var/g' . $generation . '/log',
        ];
        foreach ($paths as $path) {
            self::ensureDirectory($path);
        }
        // Supplying MAGE_DIRS replaces Magento's complete directory map;
        // setup/CLI bootstrap therefore still needs the immutable code root.
        $overrides = ['base' => ['path' => '/var/www/magento2']];
        foreach ($paths as $key => $path) {
            $overrides[$key] = ['path' => $path];
        }
        $_SERVER['MAGE_DIRS'] = $overrides;
    }

    private static function branchStateRoot(array $route): string
    {
        $base = rtrim(
            (string) (getenv('WEB_AGENT_MAGENTO_STATE_ROOT')
                ?: '/var/www/magento2/.web-agent-state'),
            '/'
        );
        foreach (['environment_id', 'branch_id'] as $key) {
            if (!preg_match('/\A[A-Za-z0-9_.-]+\z/', (string) ($route[$key] ?? ''))) {
                throw new RuntimeException("invalid route {$key}");
            }
        }
        return $base
            . '/environments/' . $route['environment_id']
            . '/branches/' . $route['branch_id'];
    }

    private static function ensureDirectory(string $path): void
    {
        $created = false;
        if (!is_dir($path)) {
            if (!mkdir($path, 0770, true) && !is_dir($path)) {
                throw new RuntimeException("cannot create routed state path: {$path}");
            }
            $created = true;
        }
        // The host lifecycle manager owns cleanup.  PHP-FPM/CLI runs as a
        // different uid but shares the host lifecycle group, so retain group
        // write permission even before Magento installs its own umask.
        // Immutable/component roots are pre-created by the host lifecycle
        // owner and need no chmod.  The request uid may chmod only directories
        // it created itself.
        if ($created && !chmod($path, 02770)) {
            throw new RuntimeException("cannot secure routed state path: {$path}");
        }
    }

    private static function base64UrlDecode(string $value): string
    {
        $padding = str_repeat('=', (4 - strlen($value) % 4) % 4);
        $decoded = base64_decode(strtr($value . $padding, '-_', '+/'), true);
        if ($decoded === false) {
            throw new InvalidArgumentException('invalid route token encoding');
        }
        return $decoded;
    }

    private static function deny(Throwable $error): never
    {
        $status = $error instanceof WebAgentMagentoRouteFrozen ? 503 : 403;
        if (PHP_SAPI !== 'cli') {
            http_response_code($status);
            header('Content-Type: text/plain');
            if ($status === 503) {
                header('Retry-After: 1');
            }
        }
        error_log('WebAgent Magento routing denied: ' . $error->getMessage());
        exit('WebAgent route denied');
    }
}

final class WebAgentMagentoRouteFrozen extends RuntimeException {}
