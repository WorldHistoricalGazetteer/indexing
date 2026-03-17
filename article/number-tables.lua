-- Lua filter for pandoc docx output.
-- pandoc wraps LaTeX \begin{table}...\end{table} blocks in Divs whose id
-- matches the \label (e.g. "tab:script-distribution").
-- The bibliography becomes Div id="refs" and ends up LAST in the AST.
-- This filter:
--   1. Identifies table Divs (contain a Table element)
--   2. Numbers their captions ("Table N: ")
--   3. Strips them from their original position
--   4. Re-inserts them (with spacing) just before the refs Div

local function is_table_div(block)
    if block.t ~= "Div" then return false end
    for _, child in ipairs(block.content) do
        if child.t == "Table" then return true end
    end
    return false
end

local function number_caption(table_el, n)
    if table_el.caption and table_el.caption.long and #table_el.caption.long > 0 then
        local prefix = pandoc.Strong("Table " .. n .. ": ")
        local first = table_el.caption.long[1]
        if first.t == "Plain" or first.t == "Para" then
            table.insert(first.content, 1, pandoc.Space())
            table.insert(first.content, 1, prefix)
        end
    end
end

function Pandoc(doc)
    local new_blocks = {}
    local table_blocks = {}  -- collected table Divs (with spacing)
    local table_count = 0
    local refs_and_after = {}
    local past_refs = false

    for _, block in ipairs(doc.blocks) do
        -- Catch the refs Div and everything after it
        if block.t == "Div" and block.attr and block.attr.identifier == "refs" then
            past_refs = true
        end
        if past_refs then
            table.insert(refs_and_after, block)
        elseif is_table_div(block) then
            -- Number the Table inside this Div
            table_count = table_count + 1
            for _, child in ipairs(block.content) do
                if child.t == "Table" then
                    number_caption(child, table_count)
                end
            end
            table.insert(table_blocks, block)
            -- Insert a page break after each table (raw OpenXML)
            table.insert(table_blocks, pandoc.RawBlock("openxml",
                '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'))
        else
            table.insert(new_blocks, block)
        end
    end

    -- Assemble: main body, then tables, then refs
    for _, b in ipairs(table_blocks) do
        table.insert(new_blocks, b)
    end
    -- Add References heading if refs Div exists
    if #refs_and_after > 0 then
        table.insert(new_blocks, pandoc.Header(1, {pandoc.Str("References")}))
    end
    for _, b in ipairs(refs_and_after) do
        table.insert(new_blocks, b)
    end

    return pandoc.Pandoc(new_blocks, doc.meta)
end








