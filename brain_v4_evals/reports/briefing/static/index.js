/* Brain V4 静态评测台脚本。
 * 只读取页面内联的 report-data，不发起任何实时请求（无 fetch / XHR）。
 */
(function () {
    'use strict';

    var dataNode = document.getElementById('report-data');
    var tbody = document.getElementById('comparison-rows');
    if (!dataNode || !tbody) {
        return;
    }

    var items = [];
    try {
        items = JSON.parse(dataNode.textContent || '[]');
    } catch (err) {
        items = [];
    }

    var dateSelect = document.getElementById('filter-date');
    var featureSelect = document.getElementById('filter-feature');
    var versionInput = document.getElementById('filter-version');

    function uniqueValues(key) {
        var seen = {};
        var values = [];
        items.forEach(function (item) {
            var value = String(item[key] || '');
            if (value && !seen[value]) {
                seen[value] = true;
                values.push(value);
            }
        });
        return values.sort();
    }

    function fillSelect(select, values) {
        if (!select) {
            return;
        }
        values.forEach(function (value) {
            var option = document.createElement('option');
            option.value = value;
            option.textContent = value;
            select.appendChild(option);
        });
    }

    fillSelect(dateSelect, uniqueValues('run_date'));
    fillSelect(featureSelect, uniqueValues('feature_id'));

    function render() {
        var date = dateSelect ? dateSelect.value : '';
        var feature = featureSelect ? featureSelect.value : '';
        var version = versionInput ? versionInput.value.trim() : '';

        tbody.innerHTML = '';
        items
            .filter(function (item) {
                if (date && item.run_date !== date) return false;
                if (feature && item.feature_id !== feature) return false;
                if (version && String(item.method_version || '').indexOf(version) === -1) return false;
                return true;
            })
            .forEach(function (item) {
                var row = document.createElement('tr');
                var cells = [
                    item.comparison_id,
                    item.feature_id,
                    item.profile,
                    item.blind_label,
                    item.snapshot_sha,
                    item.method_version,
                ];
                cells.forEach(function (value, index) {
                    var cell = document.createElement('td');
                    if (index === 0) {
                        var link = document.createElement('a');
                        link.href = item.page;
                        link.textContent = String(value);
                        cell.appendChild(link);
                    } else {
                        cell.textContent = String(value);
                    }
                    row.appendChild(cell);
                });
                tbody.appendChild(row);
            });
    }

    [dateSelect, featureSelect, versionInput].forEach(function (control) {
        if (control) {
            control.addEventListener('input', render);
            control.addEventListener('change', render);
        }
    });

    render();
})();
